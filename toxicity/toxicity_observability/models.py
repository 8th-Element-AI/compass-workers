from __future__ import annotations

import time
from typing import Any

import threading

import numpy as np
import torch
import torch._dynamo
torch._dynamo.config.disable = True  # prevent meta-tensor threading conflicts
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from . import constants as C


def resolve_device(requested: str) -> str:
    return "cuda" if requested == "cuda" and torch.cuda.is_available() else "cpu"


class HFClassifier:
    def __init__(
        self,
        model_path: str,
        *,
        device: str = "cuda",
        max_length: int = 128,
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
        self.id2label = {int(k): v for k, v in getattr(cfg, "id2label", {}).items()} if cfg else {}
        self._infer_lock = threading.Lock()

    def _raw_probs(self, text: str, sigmoid: bool = False) -> tuple[dict[str, float], float]:
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
        probs = torch.sigmoid(logits) if sigmoid else torch.softmax(logits, dim=-1)
        return {
            self.id2label.get(i, f"LABEL_{i}"): float(probs[i])
            for i in range(len(probs))
        }, round((time.time() - started) * 1000, 3)

    def _raw_probs_batch(
        self,
        texts: list[str],
        *,
        sigmoid: bool = False,
        batch_size: int = 32,
    ) -> tuple[list[dict[str, float]], float]:
        """Batched forward pass. One tokenize + one model call per chunk of
        `batch_size`. Returns per-text raw probability dicts in input order
        plus the total wall-clock latency in ms.
        """
        if not texts:
            return [], 0.0
        all_probs: list[dict[str, float]] = []
        started = time.time()
        for batch_start in range(0, len(texts), batch_size):
            chunk = texts[batch_start:batch_start + batch_size]
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
            probs_batch = torch.sigmoid(logits) if sigmoid else torch.softmax(logits, dim=-1)
            for probs in probs_batch:
                all_probs.append({
                    self.id2label.get(i, f"LABEL_{i}"): float(probs[i])
                    for i in range(len(probs))
                })
        return all_probs, round((time.time() - started) * 1000, 3)

class PromptInjectionModel(HFClassifier):
    def classify(self, text: str) -> dict[str, Any]:
        raw, latency = self._raw_probs(text)
        score = 0.0
        for label, prob in raw.items():
            low = label.lower()
            if any(tok in low for tok in ("injection", "malicious", "attack", "unsafe")):
                score = max(score, prob)
            elif label == "LABEL_1":
                score = max(score, prob)
        return {"scores": {C.PROMPT_INJECTION: score}, "raw": raw, "latency_ms": latency}

    def classify_batch(self, texts: list[str], *, batch_size: int = 32) -> list[dict[str, Any]]:
        raws, total_latency = self._raw_probs_batch(texts, batch_size=batch_size)
        per_text = round(total_latency / max(len(texts), 1), 3)
        out = []
        for raw in raws:
            score = 0.0
            for label, prob in raw.items():
                low = label.lower()
                if any(tok in low for tok in ("injection", "malicious", "attack", "unsafe")):
                    score = max(score, prob)
                elif label == "LABEL_1":
                    score = max(score, prob)
            out.append({"scores": {C.PROMPT_INJECTION: score}, "raw": raw, "latency_ms": per_text})
        return out

class ONNXPromptInjectionModel:
    def __init__(self, model_path: str, *, max_length: int = 128, **_: Any) -> None:
        import onnxruntime as ort
        self.max_length = max_length
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.session = ort.InferenceSession(
            f"{model_path}/model.onnx",
            providers=["CPUExecutionProvider"],
        )
        import json
        from pathlib import Path
        cfg_path = Path(model_path) / "config.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        self.id2label = {int(k): v for k, v in cfg.get("id2label", {}).items()}

    def classify(self, text: str) -> dict[str, Any]:
        enc = self.tokenizer(
            text,
            return_tensors="np",
            truncation=True,
            padding=True,
            max_length=self.max_length,
        )
        valid_inputs = {i.name for i in self.session.get_inputs()}
        inputs = {k: v.astype(np.int64) for k, v in enc.items() if k in valid_inputs}
        started = time.time()
        logits = self.session.run(None, inputs)[0][0]
        latency = round((time.time() - started) * 1000, 3)
        probs = np.exp(logits) / np.sum(np.exp(logits))
        raw = {self.id2label.get(i, f"LABEL_{i}"): float(probs[i]) for i in range(len(probs))}
        score = 0.0
        for label, prob in raw.items():
            low = label.lower()
            if any(tok in low for tok in ("injection", "malicious", "attack", "unsafe")):
                score = max(score, prob)
            elif label == "LABEL_1":
                score = max(score, prob)
        return {"scores": {C.PROMPT_INJECTION: score}, "raw": raw, "latency_ms": latency}

    def classify_batch(self, texts: list[str], *, batch_size: int = 32) -> list[dict[str, Any]]:
        """Per-text loop. ONNX runtime can batch but the speedup is small on CPU
        and we'd have to be careful about variable-length padding semantics.
        Left as a per-text dispatch for now; revisit if ONNX path becomes hot.
        The batch_size arg is accepted for API symmetry but unused.
        """
        return [self.classify(t) for t in texts]
    
class JailbreakModel(HFClassifier):
    def classify(self, text: str) -> dict[str, Any]:
        raw, latency = self._raw_probs(text)
        score = 0.0
        for label, prob in raw.items():
            low = label.lower()
            if any(tok in low for tok in ("jailbreak", "attack", "unsafe", "malicious")):
                score = max(score, prob)
            elif label == "LABEL_1":
                score = max(score, prob)
        return {"scores": {C.JAILBREAK: score}, "raw": raw, "latency_ms": latency}


class ModerationModel(HFClassifier):
    def classify(self, text: str) -> dict[str, Any]:
        raw, latency = self._raw_probs(text, sigmoid=True)
        harmful = 0.0
        sexual = 0.0
        for label, prob in raw.items():
            low = label.lower()
            if "sexual" in low or low in ("s", "s3"):
                sexual = max(sexual, prob)
            if (
                "harmful" in low
                or "hate" in low
                or "harassment" in low
                or "toxic" in low
                or "violence" in low
                or "self" in low
                or low in ("h", "h2", "hr", "sh", "v", "v2")
            ):
                harmful = max(harmful, prob)
            if label == "LABEL_0":
                harmful = max(harmful, prob)
            if label == "LABEL_1":
                sexual = max(sexual, prob)
        return {
            "scores": {C.HARMFUL_CONTENT: harmful, C.SEXUAL: sexual},
            "raw": raw,
            "latency_ms": latency,
        }

    def classify_batch(self, texts: list[str], *, batch_size: int = 32) -> list[dict[str, Any]]:
        raws, total_latency = self._raw_probs_batch(texts, sigmoid=True, batch_size=batch_size)
        per_text = round(total_latency / max(len(texts), 1), 3)
        out = []
        for raw in raws:
            harmful = 0.0
            sexual = 0.0
            for label, prob in raw.items():
                low = label.lower()
                if "sexual" in low or low in ("s", "s3"):
                    sexual = max(sexual, prob)
                if (
                    "harmful" in low
                    or "hate" in low
                    or "harassment" in low
                    or "toxic" in low
                    or "violence" in low
                    or "self" in low
                    or low in ("h", "h2", "hr", "sh", "v", "v2")
                ):
                    harmful = max(harmful, prob)
                if label == "LABEL_0":
                    harmful = max(harmful, prob)
                if label == "LABEL_1":
                    sexual = max(sexual, prob)
            out.append({
                "scores": {C.HARMFUL_CONTENT: harmful, C.SEXUAL: sexual},
                "raw": raw,
                "latency_ms": per_text,
            })
        return out
