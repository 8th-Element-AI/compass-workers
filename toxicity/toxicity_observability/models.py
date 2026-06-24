"""Model adapters.

Two classes:
  * PromptInjectionModel       — DeBERTa, PyTorch, softmax
  * MiniLMToxicSpamONNXModel   — MiniLM toxic/spam, ONNX, softmax

Both expose `classify(text)` and `classify_batch(texts, batch_size=...)` returning
results in the v1 shape: `{"scores": {<label>: <float>}, "raw": ..., "latency_ms": ...}`.
Compass-workers' safety lens reads `res["scores"]["prompt_injection"]` and
`res["scores"]["harmful_content"]`, so this shape is load-bearing.

Threading:
  PyTorch's DeBERTa has known meta-tensor thread-unsafety. PromptInjectionModel
  serializes inference behind `_infer_lock`. ONNX sessions are thread-safe; the
  MiniLM model does not lock.

`torch._dynamo.config.disable = True` is the same workaround v1 used to
prevent dynamo's meta-tensor state from leaking across threads.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch._dynamo
torch._dynamo.config.disable = True  # prevent meta-tensor threading conflicts

from transformers import AutoModelForSequenceClassification, AutoTokenizer

from . import constants as C


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def resolve_device(requested: str) -> str:
    """Return 'cuda' if asked AND available, else 'cpu'."""
    return "cuda" if requested == "cuda" and torch.cuda.is_available() else "cpu"


def resolve_onnx_providers(requested: str = "auto") -> list[str]:
    """Pick ORT execution providers based on request + what's installed.

    'auto' / 'cuda' / 'gpu' prefer CUDA when present, fall back to CPU.
    'cpu' forces CPU only.
    """
    import onnxruntime as ort
    available = set(ort.get_available_providers())
    requested = (requested or "auto").lower()
    if requested == "cpu":
        preferred = ["CPUExecutionProvider"]
    elif requested in ("cuda", "gpu"):
        preferred = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    else:  # auto
        preferred = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return [p for p in preferred if p in available] or ["CPUExecutionProvider"]


def _softmax(logits: np.ndarray) -> np.ndarray:
    logits = logits - np.max(logits, axis=-1, keepdims=True)
    exp = np.exp(logits)
    return exp / np.sum(exp, axis=-1, keepdims=True)


# --------------------------------------------------------------------------
# Prompt-injection model (PyTorch HF, DeBERTa)
# --------------------------------------------------------------------------
class PromptInjectionModel:
    """DeBERTa prompt-injection classifier.

    Score = max prob over labels whose lowercase contains any of
    {'inject', 'attack', 'unsafe'} OR equals 'LABEL_1'. This is v2's
    scoring rule, applied verbatim.
    """

    def __init__(
        self,
        model_path: str,
        *,
        device: str = "cuda",
        max_length: int = 512,
        fp16_on_cuda: bool = True,
    ) -> None:
        self.model_path = model_path
        self.device = resolve_device(device)
        self.max_length = max_length
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_path)
        self.model.eval()
        if self.device == "cuda":
            self.model = self.model.to("cuda")
            if fp16_on_cuda:
                self.model = self.model.half()
        cfg = getattr(self.model, "config", None)
        self.id2label = (
            {int(k): v for k, v in getattr(cfg, "id2label", {}).items()} if cfg else {}
        )
        self._infer_lock = threading.Lock()

    # ---- scoring -----------------------------------------------------
    @staticmethod
    def _score_from_raw(raw: dict[str, float]) -> float:
        score = 0.0
        for label, prob in raw.items():
            low = label.lower()
            if "inject" in low or "attack" in low or "unsafe" in low or label == "LABEL_1":
                score = max(score, prob)
        return score

    # ---- single ------------------------------------------------------
    def classify(self, text: str) -> dict[str, Any]:
        enc = self.tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            padding=True,
            max_length=self.max_length,
        )
        if self.device == "cuda":
            enc = {k: v.to("cuda") for k, v in enc.items()}
            torch.cuda.synchronize()
        started = time.time()
        with self._infer_lock, torch.inference_mode():
            logits = self.model(**enc).logits.float().detach().cpu()[0]
        if self.device == "cuda":
            torch.cuda.synchronize()
        probs = torch.softmax(logits, dim=-1)
        raw = {
            self.id2label.get(i, f"LABEL_{i}"): float(probs[i])
            for i in range(len(probs))
        }
        return {
            "scores": {C.PROMPT_INJECTION: self._score_from_raw(raw)},
            "raw": raw,
            "latency_ms": round((time.time() - started) * 1000, 3),
        }

    # ---- batched (compass-workers entry point) ------------------------
    def classify_batch(
        self,
        texts: list[str],
        *,
        batch_size: int = 32,
    ) -> list[dict[str, Any]]:
        """Batched forward pass. One tokenize + one model call per chunk of
        `batch_size`. Returns results in input order.
        """
        if not texts:
            return []
        all_raw: list[dict[str, float]] = []
        started = time.time()
        for start in range(0, len(texts), batch_size):
            chunk = texts[start:start + batch_size]
            enc = self.tokenizer(
                chunk,
                return_tensors="pt",
                truncation=True,
                padding=True,
                max_length=self.max_length,
            )
            if self.device == "cuda":
                enc = {k: v.to("cuda") for k, v in enc.items()}
                torch.cuda.synchronize()
            with self._infer_lock, torch.inference_mode():
                logits = self.model(**enc).logits.float().detach().cpu()
            if self.device == "cuda":
                torch.cuda.synchronize()
            probs_batch = torch.softmax(logits, dim=-1)
            for probs in probs_batch:
                all_raw.append({
                    self.id2label.get(i, f"LABEL_{i}"): float(probs[i])
                    for i in range(len(probs))
                })
        per_text_latency = round((time.time() - started) * 1000 / max(len(texts), 1), 3)
        return [
            {
                "scores": {C.PROMPT_INJECTION: self._score_from_raw(raw)},
                "raw": raw,
                "latency_ms": per_text_latency,
            }
            for raw in all_raw
        ]


# --------------------------------------------------------------------------
# Moderation model (MiniLM toxic/spam, ONNX)
# --------------------------------------------------------------------------
class MiniLMToxicSpamONNXModel:
    """MiniLM toxic-spam classifier (ONNX) mapped to HARMFUL_CONTENT.

    Score = max prob over labels whose lowercase is in `harmful_labels`.
    Default `harmful_labels = ("toxic", "spam")` matches v2.

    Expects the HF snapshot layout:
      <model_path>/config.json
      <model_path>/onnx/model.onnx
      <model_path>/tokenizer.json   (and friends)
    """

    def __init__(
        self,
        model_path: str,
        *,
        max_length: int = 512,
        onnx_provider: str = "auto",
        harmful_labels: tuple[str, ...] = ("toxic", "spam"),
        **_: Any,
    ) -> None:
        import onnxruntime as ort

        self.model_path = model_path
        self.max_length = max_length
        self.harmful_labels = {label.lower() for label in harmful_labels}
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        cfg = json.loads((Path(model_path) / "config.json").read_text(encoding="utf-8"))
        self.id2label = {int(k): v for k, v in cfg.get("id2label", {}).items()}
        self.providers = resolve_onnx_providers(onnx_provider)
        self.session = ort.InferenceSession(
            str(Path(model_path) / "onnx" / "model.onnx"),
            providers=self.providers,
        )
        self.input_names = {inp.name for inp in self.session.get_inputs()}

    # ---- scoring -----------------------------------------------------
    def _score_from_raw(self, raw: dict[str, float]) -> float:
        return max(
            (prob for label, prob in raw.items() if label.lower() in self.harmful_labels),
            default=0.0,
        )

    # ---- single ------------------------------------------------------
    def classify(self, text: str) -> dict[str, Any]:
        enc = self.tokenizer(
            text,
            return_tensors="np",
            truncation=True,
            padding=True,
            max_length=self.max_length,
        )
        feed = {k: v.astype("int64") for k, v in enc.items() if k in self.input_names}
        started = time.time()
        logits = self.session.run(None, feed)[0]
        probs = _softmax(logits)[0]
        raw = {
            self.id2label.get(i, f"LABEL_{i}"): float(probs[i])
            for i in range(len(probs))
        }
        return {
            "scores": {C.HARMFUL_CONTENT: self._score_from_raw(raw)},
            "raw": {"scores": raw, "providers": self.session.get_providers()},
            "latency_ms": round((time.time() - started) * 1000, 3),
        }

    # ---- batched (compass-workers entry point) ------------------------
    def classify_batch(
        self,
        texts: list[str],
        *,
        batch_size: int = 32,
    ) -> list[dict[str, Any]]:
        """Batched ONNX inference. ORT happily accepts a (B, T) input batch with
        padding; one tokenize + one session.run per chunk.
        """
        if not texts:
            return []
        out: list[dict[str, Any]] = []
        providers = self.session.get_providers()
        started = time.time()
        for start in range(0, len(texts), batch_size):
            chunk = texts[start:start + batch_size]
            enc = self.tokenizer(
                chunk,
                return_tensors="np",
                truncation=True,
                padding=True,
                max_length=self.max_length,
            )
            feed = {k: v.astype("int64") for k, v in enc.items() if k in self.input_names}
            logits = self.session.run(None, feed)[0]
            probs_batch = _softmax(logits)
            for probs in probs_batch:
                raw = {
                    self.id2label.get(i, f"LABEL_{i}"): float(probs[i])
                    for i in range(len(probs))
                }
                out.append({
                    "scores": {C.HARMFUL_CONTENT: self._score_from_raw(raw)},
                    "raw": {"scores": raw, "providers": providers},
                    "latency_ms": 0.0,  # filled below
                })
        per_text_latency = round((time.time() - started) * 1000 / max(len(texts), 1), 3)
        for r in out:
            r["latency_ms"] = per_text_latency
        return out