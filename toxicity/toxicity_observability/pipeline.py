from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import constants as C
from .config import load_config, resolve_path
from .deterministic import ATTACK, MODERATION, evaluate as deterministic_gate
from .fasttext_router import FastTextRouter
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
        self._fasttext: FastTextRouter | None = None
        self._prompt_injection: Any = None
        self._moderation: ModerationModel | None = None

    @property
    def _common_kwargs(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "max_length": self.max_length,
            "fp16_on_cuda": self.fp16_on_cuda,
        }
    
    @property
    def fasttext(self) -> FastTextRouter:
        if self._fasttext is None:
            self._fasttext = FastTextRouter(
                resolve_path(self._models_cfg["fasttext_router"]["local_path"])
            )
        return self._fasttext
    
    @property
    def prompt_injection(self):
        if self._prompt_injection is None:
            onnx_path = resolve_path(self._models_cfg["prompt_injection_onnx_int8"]["local_path"])
            if self.device == "cpu" and (onnx_path / "model.onnx").exists():
                self._prompt_injection = ONNXPromptInjectionModel(
                    str(onnx_path), **self._common_kwargs
                )
            else:
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
        started = time.time()
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        thresholds = self.config["thresholds"]
        full_scan = self.config.get("runtime", {}).get("full_scan_default", False) if full_scan is None else full_scan
        norm = normalize(text)
        rules = deterministic_gate(norm)
        ft = self.fasttext.predict(norm.detection_text)
        ft_scores = ft["scores"]

        run_attack = full_scan or ATTACK in rules.force_route or ft_scores["attack"] >= thresholds["attack_route"]
        run_moderation = full_scan or MODERATION in rules.force_route or ft_scores["moderation"] >= thresholds["moderation_route"]
        fast_allow = (
            not full_scan
            and rules.allow_fast_skip
            and ft_scores["attack"] < thresholds["fast_allow"]
            and ft_scores["moderation"] < thresholds["fast_allow"]
        )

        scores = {label: 0.0 for label in C.PUBLIC_LABELS}
        raw: dict[str, Any] = {"fasttext": ft, "rules": {"routes": sorted(rules.force_route), "reasons": rules.reasons}}
        triggered: list[str] = ["fasttext_router"] if self.fasttext.loaded else []

        ft_direct = thresholds.get("fasttext_direct", 0.99)
        ft_direct_attack = not full_scan and ft_scores["attack"] >= ft_direct
        ft_direct_moderation = not full_scan and ft_scores["moderation"] >= ft_direct

        if ft_direct_attack:
            scores[C.PROMPT_INJECTION] = ft_scores["attack"]
        elif run_attack and not fast_allow:
            pi = self.prompt_injection.classify(norm.model_text)
            scores.update(pi["scores"])
            triggered.append("prompt_injection")
            if include_raw:
                raw["prompt_injection"] = pi["raw"]

        if ft_direct_moderation:
            scores[C.HARMFUL_CONTENT] = ft_scores["moderation"]
        elif run_moderation and not fast_allow:
            mod = self.moderation.classify(norm.model_text)
            scores.update(mod["scores"])
            triggered.append("moderation")
            if include_raw:
                raw["moderation"] = mod["raw"]

        labels = []
        if scores[C.PROMPT_INJECTION] >= thresholds["prompt_injection_review"]:
            labels.append(C.PROMPT_INJECTION)
        if scores[C.HARMFUL_CONTENT] >= thresholds["harmful_content_review"]:
            labels.append(C.HARMFUL_CONTENT)
        if scores[C.SEXUAL] >= thresholds["sexual_review"]:
            labels.append(C.SEXUAL)

        return {
            "labels": labels,
            "scores": {k: round(v, 4) for k, v in scores.items()},
            "fast_allow": fast_allow,
            "triggered_models": triggered,
            "skipped_models": [
                name for name in ("prompt_injection", "moderation") if name not in triggered
            ],
            "routing": {
                "fasttext": {k: round(v, 4) for k, v in ft_scores.items()},
                "rule_reasons": rules.reasons,
                "run_attack": run_attack,
                "run_moderation": run_moderation,
            },
            "latency_ms": round((time.time() - started) * 1000, 2),
            "raw": raw if include_raw else None,
        }
