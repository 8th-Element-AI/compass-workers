from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import constants as C
from .config import load_config, resolve_path
from .deterministic import ATTACK, MODERATION, evaluate as deterministic_gate
from .models import ModerationModel, ONNXPromptInjectionModel, PromptInjectionModel, resolve_device
from .normalize import normalize


class ToxicityClassifier:
    """ENd-to-end toxicity classifier.
    
    Models are loaded lazily - accessing `.fasttext`, `.prompt_injection`, or
    `.moderation` triggers a one-time load of that model only. Callers that
    only need one model (e.g. the saftey lens worker, which drives routing
    itself) can read individual properties without paying for the others.
    """

    
    def __init__(
        self,
        config_path: str | None = None,
        *,
        config_dict: dict | None = None,
    ) -> None:
        """Initialize with either a config file path or a pre-built dict.

        Precedence:
          * `config_dict` if provided (used by signal-workers, which builds
            it from env-driven pydantic Settings),
          * else `load_config(config_path)` — yaml file, default for CLI use.

        The internal `self.config` shape is identical either way, so
        downstream code (`classify`, the lazy model properties) is unchanged.
        """
        if config_dict is not None:
            self.config = dict(config_dict)
        else:
            self.config = load_config(config_path)
            
        runtime = self.config.get("runtime", {})
        self.device = resolve_device(runtime.get("device", "cuda"))
        self.max_length = int(runtime.get("max_length", 128))
        self.fp16_on_cuda = bool(runtime.get("fp16_on_cuda", True))
        self._models_cfg = self.config["models"]

        # Lazy slots - None until the corresponding property is read.
        self._prompt_injection: PromptInjectionModel | ONNXPromptInjectionModel | None = None
        self._moderation:       ModerationModel | None = None

    @property
    def _common_kwargs(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "max_length": self.max_length,
            "fp16_on_cuda": self.fp16_on_cuda,
        }
        
    @property
    def prompt_injection(self):
        if self._prompt_injection is None:
            # CPU-only and ONNX variant available → use it (faster on CPU)
            if self.device == "cpu" and "prompt_injection_onnx_int8" in self._models_cfg:
                onnx_path = resolve_path(self._models_cfg["prompt_injection_onnx_int8"]["local_path"])
                if Path(onnx_path).exists():
                    self._prompt_injection = ONNXPromptInjectionModel(
                        model_path=onnx_path, max_length=self.max_length,
                    )
                    return self._prompt_injection
            self._prompt_injection = PromptInjectionModel(
                str(resolve_path(self._models_cfg["prompt_injection"]["local_path"])),
                **self._common_kwargs,
            )
        return self._prompt_injection

    @property
    def moderation(self) -> ModerationModel:
        if self._moderation is None:
            self._moderation = ModerationModel(
                str(resolve_path(self._models_cfg["moderation"]["local_path"])),
                **self._common_kwargs,
            )
        return self._moderation

    def classify(self, text: str, *, full_scan: bool | None = None, include_raw: bool = False) -> dict[str, Any]:
        """Classify a single text.

        Pipeline (FastText removed):
          1. Normalize.
          2. Deterministic rules. If a rule fires for a label, that label's
             score is set to 1.0 and the corresponding BERT is skipped.
          3. For any label the rules did NOT force, run the corresponding
             BERT and use its score.
          4. Emit labels for scores >= review threshold.

        full_scan=True bypasses rule short-circuits — every BERT runs
        unconditionally. Useful for evaluation / debugging.
        """
        started = time.time()
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        
        thresholds = self.config["thresholds"]
        if full_scan is None:
            full_scan = self.config.get("runtime", {}).get("full_scan_default", False)

        norm = normalize(text)
        rules = deterministic_gate(norm)

        scores: dict[str, float] = {label: 0.0 for label in C.PUBLIC_LABELS}
        triggered: list[str] = []
        skipped: list[str] = []
        raw: dict[str, Any] = {
            "rules": {
                "routes":  sorted(rules.force_route),
                "labels":  sorted(rules.force_label),
                "reasons": rules.reasons,
            }
        }

        # -------- Prompt-injection decision --------
        pi_forced = (C.PROMPT_INJECTION in rules.force_label) and not full_scan
        if pi_forced:
            scores[C.PROMPT_INJECTION] = 1.0
            skipped.append("prompt_injection")
        else:
            pi = self.prompt_injection.classify(norm.model_text)
            scores[C.PROMPT_INJECTION] = pi["scores"][C.PROMPT_INJECTION]
            triggered.append("prompt_injection")
            if include_raw:
                raw["prompt_injection"] = pi

        # -------- Moderation decision --------
        harmful_forced = (C.HARMFUL_CONTENT in rules.force_label) and not full_scan

        if harmful_forced:
            # Set the forced labels to 1.0.
            scores[C.HARMFUL_CONTENT] = 1.0
            skipped.append("moderation")
        else:
            mod = self.moderation.classify(norm.model_text)
            scores[C.HARMFUL_CONTENT] = mod["scores"][C.HARMFUL_CONTENT]
            triggered.append("moderation")
            if include_raw:
                raw["moderation"] = mod

        # -------- Labels above review threshold --------
        labels: list[str] = []
        for label, key in [
            (C.PROMPT_INJECTION, "prompt_injection_review"),
            (C.HARMFUL_CONTENT,  "harmful_content_review"),
        ]:
            if scores[label] >= thresholds[key]:
                labels.append(label)

        return {
            "labels":            labels,
            "scores":            scores,
            "triggered_models":  triggered,
            "skipped_models":    skipped,
            "rules": {
                "routes":  sorted(rules.force_route),
                "labels":  sorted(rules.force_label),
                "reasons": rules.reasons,
            },
            "latency_ms": round((time.time() - started) * 1000, 2),
            "raw": raw if include_raw else None,
        }