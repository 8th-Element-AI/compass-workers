from __future__ import annotations

import time
from typing import Any

from . import constants as C
from .config import load_config, resolve_path
from .models import MiniLMToxicSpamONNXModel, PromptInjectionModel, resolve_device


class ToxicityClassifier:
    """End-to-end toxicity classifier.

    Pipeline:
      1. Validate the text is a non-empty string.
      2. Run the prompt-injection model (DeBERTa, PyTorch).
      3. Run the moderation model (MiniLM toxic/spam, ONNX).
      4. Emit labels for scores >= review threshold.

    Both models run on every call.

    Lazy loading:
      `prompt_injection` and `moderation` are properties. The underlying
      model weights are loaded on first access. A worker that only ever
      needs one of them never instantiates the other.
    """

    def __init__(
        self,
        config_path: str | None = None,
        *,
        config_dict: dict | None = None,
        device: str | None = None,
        onnx_provider: str | None = None,
        max_length: int | None = None,
    ) -> None:
        """Initialize from a YAML file or a pre-built dict.

        Precedence:
          * `config_dict` if provided (signal-workers builds it from env-driven
            pydantic Settings),
          * else `load_config(config_path)` (yaml file, default for CLI).

        The keyword overrides (`device`, `onnx_provider`, `max_length`) are
        applied on top of whichever config is used.
        """
        if config_dict is not None:
            self.config = dict(config_dict)
        else:
            self.config = load_config(config_path)

        runtime = self.config.setdefault("runtime", {})
        if device is not None:
            runtime["device"] = device
        if onnx_provider is not None:
            runtime["onnx_provider"] = onnx_provider
        if max_length is not None:
            runtime["max_length"] = max_length

        self.device = resolve_device(runtime.get("device", "cuda"))
        self.max_length = int(runtime.get("max_length", 512))
        self.onnx_provider = runtime.get("onnx_provider", "auto")
        self.fp16_on_cuda = bool(runtime.get("fp16_on_cuda", True))
        self._models_cfg = self.config["models"]

        # Lazy slots — None until the property is read.
        self._prompt_injection: PromptInjectionModel | None = None
        self._moderation: MiniLMToxicSpamONNXModel | None = None

    # ----- lazy model properties --------------------------------------
    @property
    def prompt_injection(self) -> PromptInjectionModel:
        if self._prompt_injection is None:
            self._prompt_injection = PromptInjectionModel(
                str(resolve_path(self._models_cfg["prompt_injection"]["local_path"])),
                device=self.device,
                max_length=self.max_length,
                fp16_on_cuda=self.fp16_on_cuda,
            )
        return self._prompt_injection

    @property
    def moderation(self) -> MiniLMToxicSpamONNXModel:
        if self._moderation is None:
            self._moderation = MiniLMToxicSpamONNXModel(
                str(resolve_path(self._models_cfg["moderation"]["local_path"])),
                max_length=self.max_length,
                onnx_provider=self.onnx_provider,
            )
        return self._moderation

    # ----- classify ---------------------------------------------------
    def classify(self, text: str, *, include_raw: bool = False) -> dict[str, Any]:
        """Run both models on `text`, return labels + scores."""
        started = time.time()
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")

        prompt = self.prompt_injection.classify(text)
        moderation = self.moderation.classify(text)
        thresholds = self.config["thresholds"]

        scores: dict[str, float] = {
            C.PROMPT_INJECTION: prompt["scores"][C.PROMPT_INJECTION],
            C.HARMFUL_CONTENT:  moderation["scores"][C.HARMFUL_CONTENT],
        }

        labels: list[str] = []
        if scores[C.PROMPT_INJECTION] >= thresholds["prompt_injection_review"]:
            labels.append(C.PROMPT_INJECTION)
        if scores[C.HARMFUL_CONTENT] >= thresholds["harmful_content_review"]:
            labels.append(C.HARMFUL_CONTENT)

        result: dict[str, Any] = {
            "labels": labels,
            "scores": {k: round(v, 4) for k, v in scores.items()},
            "triggered_models": ["prompt_injection", "moderation"],
            "runtime": {
                "device": self.device,
                "onnx_provider": self.onnx_provider,
                "onnx_providers_active": moderation["raw"]["providers"],
                "max_length": self.max_length,
            },
            "latency_ms": round((time.time() - started) * 1000, 2),
            "model_latency_ms": {
                "prompt_injection": prompt["latency_ms"],
                "moderation":       moderation["latency_ms"],
            },
        }
        if include_raw:
            result["raw"] = {
                "prompt_injection": prompt["raw"],
                "moderation":       moderation["raw"],
            }
        return result