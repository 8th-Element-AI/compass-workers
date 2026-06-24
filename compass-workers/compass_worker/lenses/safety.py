"""Safety lens — PII + toxicity (prompt injection, moderation).

Decomposed into three independently gated analysis steps:

  pii              — Presidio over input + output  -> pii_count, pii_detected
  prompt_injection — DeBERTa PI over input         -> prompt_injection_detected
  moderation       — MiniLM toxic/spam ONNX over input + output -> toxicity_detected

Per-batch flow:

  process_batch(spans)
    -> Stage 1 gate (drop spans with no active safety toggle)
    -> for each step:
         spans_needing = sub-filter by step.metrics
         if empty: skip step entirely (its model NEVER loads)
         else: extract texts, dedup, step.analyze() in one batched call, cache
    -> for each kept span: build_context reads all 3 caches -> compute -> emit

Each step's model loads only the first time it's actually called with non-empty
input. If your PG thresholds only enable `pii_count`, the prompt-injection and
moderation steps' `_spans_needing()` returns [] and both BERTs stay on disk.
Same idea for `prompt_injection_detected` vs `toxicity_detected` — toggle one,
the other model is untouched.

Migration note (v0.2.0 toxicity_observability):
  The `toxicity_router` step and its FastText / deterministic-rules logic were
  removed. Both BERTs now run on every text that reaches the analyzer (the
  Stage-1 gate still skips spans with no active toggles). v2 of the toxicity
  package proved out the simpler always-run-both pipeline; this lens follows.
"""
from __future__ import annotations
 
import logging
import time
 
from ..base import parse_meta
from ..spec import MetricSpec, PrefillStep, SpecWorker
from ..utils import LRUCache
from ..patterns import ctx_value
from ..predicates import llm_call
 
log = logging.getLogger("compass.worker.safety")

LENS = "safety"

# These sets drive the toggle-gate sub-filtering (_spans_needing(kept, step.metrics)).
# They MUST match metric names in SPECS exactly. Adding a metric requires updating
# both the relevant set here and the SPECS list below.
PII_METRICS              = {"pii_detected"}
PROMPT_INJECTION_METRICS = {"prompt_injection_detected"}
MODERATION_METRICS       = {"toxicity_detected"}

def _spec(metric, applies, pattern, inputs, unit, window="1h",
          threshold=False, per_span=True, meta_fn=None):
    return MetricSpec(
        metric=metric, lens=LENS, applies=applies, pattern=pattern,
        inputs=inputs, unit=unit, window=window, threshold=threshold,
        per_span=per_span, meta_fn=meta_fn,
    )

# ---- meta_fn: attach per-row detail to metric_meta -----------------------------

def _pii_meta(span, ctx):
    """Per-row PII detail attached to metric_meta.

    Always emitted when anything was detected (risk-flagged or not). Carries:
      - count       : risk-filtered entity count (was the old pii_count value)
      - severity    : max severity across input/output (none|low|medium|high|phi)
      - types       : risk-filtered entity types (only when pii_detected=1)
      - location    : input | output | both (only when pii_detected=1)
      - violations  : list of {rule, severity, location, entity_types}
                      (only when pii_detected=1)
      - raw_count   : pre-filter detection count (only when it differs from count)
      - raw_types   : pre-filter detection types (only when raw_count differs)

    Returns None when there's nothing to report at all (no detection, no raw).
    """
    detected  = ctx.get("pii_detected", 0.0) > 0.0
    count     = int(ctx.get("pii_count", 0))
    raw_count = int(ctx.get("pii_raw_count", 0))

    if not detected and raw_count == 0:
        return None

    meta = {
        "count":    count,
        "severity": ctx.get("pii_severity", "none"),
    }

    if detected:
        meta["types"] = ctx.get("pii_types", [])
        if ctx.get("pii_location"):
            meta["location"] = ctx["pii_location"]
        if ctx.get("pii_violations"):
            meta["violations"] = ctx["pii_violations"]

    # Diagnostic: surface raw layer when filter dropped detections
    if raw_count != count:
        meta["raw_count"] = raw_count
        meta["raw_types"] = ctx.get("pii_raw_types", [])

    return meta

def _prompt_injection_meta(span, ctx):
    """Per-row PI detail.

    Fields:
      prompt_injection_score : final score from the PI BERT
      pi_bert_ran            : did PI BERT actually execute for this text?
      triggered_models       : models that ran end-to-end on this span
    """
    score  = float(ctx.get("prompt_injection_score", 0.0))
    pi_ran = bool(ctx.get("prompt_injection_ran", False))

    return {
        "prompt_injection_score": round(score, 4),
        "pi_bert_ran":            pi_ran,
        "triggered_models":       ctx.get("triggered_models", []),
    }

def _toxicity_meta(span, ctx):
    """Per-row moderation detail. Same shape pattern as _prompt_injection_meta."""
    harmful = float(ctx.get("harmful_content_score", 0.0))
    mod_ran = bool(ctx.get("moderation_ran", False))

    meta = {
        "harmful_content_score": round(harmful, 4),
        "moderation_bert_ran":   mod_ran,
        "triggered_models":      ctx.get("triggered_models", []),
    }
    if ctx.get("moderation_location"):
        meta["moderation_location"] = ctx["moderation_location"]
    return meta


SPECS = [
    # PII
    _spec("pii_detected", llm_call, ctx_value("pii_detected"),
          ["pii_count > 0"],
          unit="ratio", window="1h", threshold=True, meta_fn=_pii_meta),

    # Prompt injection (input only)
    _spec("prompt_injection_detected", llm_call, ctx_value("prompt_injection_detected"),
          ["metadata.input -> prompt_injection BERT"],
          unit="ratio", window="1h", threshold=True, meta_fn=_prompt_injection_meta),

    # Toxicity (harmful via MiniLM moderation, max across input + output)
    _spec("toxicity_detected", llm_call, ctx_value("toxicity_detected"),
          ["metadata.input + metadata.output -> moderation BERT"],
          unit="ratio", window="1h", threshold=True, meta_fn=_toxicity_meta),
]

# ---- text extractors ----------------------------------------------------------

def _extract_input_and_output(span):
    """PII + moderation both use this — they need both sides of the conversation."""
    md = parse_meta(span.get("metadata"))
    return (md.get("input"), md.get("output"))

def _extract_input_only(span):
    """prompt_injection only — model output isn't a prompt-injection attack."""
    md = parse_meta(span.get("metadata"))
    return (md.get("input"),)


class SafetyWorker(SpecWorker):
    """Safety lens — PII + toxicity, three lazy-loaded analysis steps.

    See module docstring for the per-batch flow. Each step runs only if at
    least one span in the batch has a matching active toggle, and the
    underlying model is constructed only on first call to that step's
    analyze() — so a worker that never sees an active `toxicity_detected`
    toggle never instantiates the moderation BERT.
    """
    lens = LENS
    specs = SPECS
    span_types = ("model_call",)

    def __init__(self, cfg, toggle_cache=None):
        super().__init__(cfg, toggle_cache=toggle_cache)

        # PII config
        self.pii_batch_size = cfg.compass_pii_batch_size
        self.ner_model = cfg.compass_pii_ner_model

        self.toxicity_batch_size = cfg.compass_toxicity_batch_size

        # Lazy holders. _tox is just a config + lazy model registry — its own
        # internal .prompt_injection / .moderation properties load the actual
        # weights on first access.
        self._pii_engine = None
        self._tox = None

        # Result caches — one per analyzer.
        self._pii_cache = LRUCache(cfg.compass_pii_cache_max)
        self._pi_cache  = LRUCache(cfg.compass_toxicity_cache_max)
        self._mod_cache = LRUCache(cfg.compass_toxicity_cache_max)

        self._steps = [
            PrefillStep(
                name="pii",
                metrics=PII_METRICS,
                cache=self._pii_cache,
                extract=_extract_input_and_output,
                analyze=self._analyze_pii,
            ),
            PrefillStep(
                name="prompt_injection",
                metrics=PROMPT_INJECTION_METRICS,
                cache=self._pi_cache,
                extract=_extract_input_only,
                analyze=self._analyze_prompt_injection,
            ),
            PrefillStep(
                name="moderation",
                metrics=MODERATION_METRICS,
                cache=self._mod_cache,
                extract=_extract_input_and_output,
                analyze=self._analyze_moderation,
            ),
        ]

        self._verify_models_ready()

    # ------------------------------------------------------------------
    # Lazy holders
    # ------------------------------------------------------------------
    @property
    def pii_engine(self):
        if self._pii_engine is None:
            from deidentifier import PresidioEngine
            log.info("[safety] loading PresidioEngine (ner_model=%s)", self.ner_model)
            self._pii_engine = PresidioEngine.get_instance(ner_model=self.ner_model)
        return self._pii_engine

    @property
    def tox(self):
        """ToxicityClassifier: config holder + lazy model registry. Constructing
        it touches no model weights; accessing .prompt_injection / .moderation
        is what loads them.

        Config comes from pydantic Settings (env-driven), not runtime.yaml.
        """
        if self._tox is None:
            from toxicity_observability import ToxicityClassifier
            cfg_dict = self._build_toxicity_config()
            log.info(
                "[safety] loading ToxicityClassifier (device=%s, onnx_provider=%s, models_root=%s)",
                cfg_dict["runtime"]["device"],
                cfg_dict["runtime"]["onnx_provider"],
                self.cfg.compass_toxicity_models_root,
            )
            self._tox = ToxicityClassifier(config_dict=cfg_dict)
        return self._tox

    def _build_toxicity_config(self):
        """Project pydantic Settings into the dict shape ToxicityClassifier expects.

        Model paths: absolute paths pass through, relative ones are joined onto
        `compass_toxicity_models_root`. This way one env var (the root) parks the
        whole model tree in a container, and the two individual path fields
        rarely need to be touched.
        """
        from pathlib import Path
        c = self.cfg
        root = Path(c.compass_toxicity_models_root)

        def _resolve(rel: str) -> str:
            p = Path(rel)
            return str(p if p.is_absolute() else root / p)

        return {
            "models": {
                "prompt_injection": {"local_path": _resolve(c.compass_toxicity_pi_path)},
                "moderation":       {"local_path": _resolve(c.compass_toxicity_mod_path)},
            },
            "thresholds": {
                "prompt_injection_review": c.compass_toxicity_pi_threshold,
                "harmful_content_review":  c.compass_toxicity_harmful_threshold,
            },
            "runtime": {
                "device":        c.compass_toxicity_device,
                "onnx_provider": c.compass_toxicity_onnx_provider,
                "max_length":    c.compass_toxicity_max_length,
                "fp16_on_cuda":  c.compass_toxicity_fp16,
            },
        }

    # ------------------------------------------------------------------
    # Step analyzers — the `analyze` callables on each PrefillStep.
    # The engine passes texts that are unique and not yet in THAT step's cache.
    # ------------------------------------------------------------------

    def _analyze_pii(self, texts):
        log.info("[safety:pii] analyzing %d unique texts", len(texts))
        return self.pii_engine.analyze_batch(
            texts, batch_size=self.pii_batch_size,
        )

    def _analyze_prompt_injection(self, texts):
        """PI BERT over every input text. Model loads lazily on first call to
        self.tox.prompt_injection.classify_batch().
        """
        if not texts:
            return []
        log.info("[safety:pi] PI BERT over %d texts", len(texts))
        results = self.tox.prompt_injection.classify_batch(
            texts, batch_size=self.toxicity_batch_size,
        )
        return [
            {
                "score": float(res["scores"]["prompt_injection"]),
                "ran":   True,
            }
            for res in results
        ]

    def _analyze_moderation(self, texts):
        """Moderation MiniLM ONNX over every input + output text. Model loads
        lazily on first call to self.tox.moderation.classify_batch().
        """
        if not texts:
            return []
        log.info("[safety:mod] Moderation BERT over %d texts", len(texts))
        results = self.tox.moderation.classify_batch(
            texts, batch_size=self.toxicity_batch_size,
        )
        return [
            {
                "harmful": float(res["scores"]["harmful_content"]),
                "ran":     True,
            }
            for res in results
        ]

    # ------------------------------------------------------------------
    # process_batch: gate -> loop over steps -> shared engine
    # ------------------------------------------------------------------
    def process_batch(self, spans):
        t_start = time.time()
        original_count = len(spans)

        # Stage 1 — drop spans with no active safety threshold
        kept = self.filter_spans_by_gate(spans)
        skipped_at_gate = original_count - len(kept)
        if skipped_at_gate:
            log.info("[safety] %d/%d spans skipped at gate before any analysis",
                     skipped_at_gate, original_count)
        if not kept:
            return []

        # Run each registered prefill — generic, time it uniformly.
        # _prefill_cache returns 0 (and logs) when no spans need that step, so
        # the model stays unloaded.
        step_timings = []
        for step in self._steps:
            t = time.time()
            n_texts = self._prefill_cache(kept, step)
            step_timings.append(
                (step.name, round((time.time() - t) * 1000, 1), n_texts)
            )

        # Stage 2 (in compute) + emit, via shared engine
        t_emit = time.time()
        rows = self._process_kept(original_count, kept, skipped_at_gate)
        emit_ms = round((time.time() - t_emit) * 1000, 1)

        total_ms = round((time.time() - t_start) * 1000, 1)
        step_str = " | ".join(f"{n}={ms}ms ({k} texts)" for n, ms, k in step_timings)

        log.info(
            "[safety] latency | total=%.1fms | %s | emit=%.1fms | rows=%d",
            total_ms, step_str, emit_ms, len(rows),
        )
        return rows

    def _verify_models_ready(self) -> None:
        """Validate every model artifact at startup, before accepting batches.

        Catches the entire class of silent failures where a model fails to
        load (wrong path, missing file, corrupted weights, missing HF cache)
        and the worker happily emits zero-score metrics that look like
        "evaluated, clean" but are actually "evaluated, broken."

        Called from __init__. Crashes loud on any problem.
        """
        from pathlib import Path
        c = self.cfg

        log.info("[safety] startup health check: verifying model artifacts...")

        # -- Toxicity models -----------------------------------------------
        root = Path(c.compass_toxicity_models_root)

        def _resolve(rel: str) -> Path:
            p = Path(rel)
            return p if p.is_absolute() else root / p

        tox_artifacts = [
            ("prompt_injection", _resolve(c.compass_toxicity_pi_path),  "dir", True),
            ("moderation",       _resolve(c.compass_toxicity_mod_path), "dir", True),
        ]

        missing = []
        for name, path, kind, required in tox_artifacts:
            if not path.exists():
                if required:
                    missing.append((name, path))
                    log.error("[safety] FAIL: %s missing at %s", name, path)
                else:
                    log.info("[safety]  OK: %s NOT found at %s (optional, will fall back)", name, path)
                continue
            if kind == "file":
                size_mb = path.stat().st_size / (1024 * 1024)
                log.info("[safety]  OK: %-25s %s  (%.1f MB)", name, path, size_mb)
            else:
                n_files = sum(1 for _ in path.rglob("*") if _.is_file())
                log.info("[safety]  OK: %-25s %s  (%d files)", name, path, n_files)

        # -- Extra check: moderation needs the ONNX file inside the snapshot --
        if not missing:
            mod_root = _resolve(c.compass_toxicity_mod_path)
            onnx_file = mod_root / "onnx" / "model.onnx"
            if not onnx_file.exists():
                missing.append(("moderation/onnx/model.onnx", onnx_file))
                log.error("[safety] FAIL: moderation ONNX file missing at %s", onnx_file)
            else:
                size_mb = onnx_file.stat().st_size / (1024 * 1024)
                log.info("[safety]  OK: %-25s %s  (%.1f MB)",
                         "moderation/onnx", onnx_file, size_mb)

        # -- PII NER model in HF cache -------------------------------------
        if c.compass_pii_ner_model and "/" in c.compass_pii_ner_model:
            try:
                from huggingface_hub import try_to_load_from_cache
                cached = try_to_load_from_cache(c.compass_pii_ner_model, "config.json")
                if cached is None:
                    missing.append(("pii_ner_model (HF cache)", Path(c.compass_pii_ner_model)))
                    log.error("[safety] FAIL: PII NER model '%s' not in HF cache",
                            c.compass_pii_ner_model)
                else:
                    log.info("[safety]  OK: %-25s %s",
                            "pii_ner_model", c.compass_pii_ner_model)
            except ImportError:
                log.warning("[safety] huggingface_hub not installed — skipping PII NER cache check")

        # -- Bail loud if anything was missing -----------------------------
        if missing:
            lines = [
                f"Safety worker startup failed: {len(missing)} required artifact(s) not found:",
                *(f"  - {name}: {path}" for name, path in missing),
                "",
                "Common fixes:",
                f"  - Toxicity models: `toxicity-observe download` or set COMPASS_TOXICITY_MODELS_ROOT correctly (current: {root})",
                f"  - PII NER: python -c \"from transformers import pipeline; "
                f"pipeline('ner', model='{c.compass_pii_ner_model}', aggregation_strategy='first')\"",
            ]
            raise FileNotFoundError("\n".join(lines))

        log.info("[safety] startup health check: PASSED")

    # ------------------------------------------------------------------
    # build_context — read all 3 caches, summarize per span
    # ------------------------------------------------------------------
    def build_context(self, span):
        md = parse_meta(span.get("metadata"))
        input_text  = md.get("input")  or ""
        output_text = md.get("output") or ""

        # ---- PII (input + output) ----
        in_pii  = self._pii_cache.get(self._hash(input_text))  if input_text  else None
        out_pii = self._pii_cache.get(self._hash(output_text)) if output_text else None

        # Risk-filtered counts and types (what survived severity >= MEDIUM)
        in_count  = in_pii.entity_count  if in_pii  else 0
        out_count = out_pii.entity_count if out_pii else 0
        total_pii = in_count + out_count

        types = {}
        for res in (in_pii, out_pii):
            if res:
                for t, c in res.entities.items():
                    types[t] = types.get(t, 0) + c

        # Raw (pre-filter) counts and types — useful for tuning and debugging
        in_raw_count  = in_pii.raw_entity_count  if in_pii  else 0
        out_raw_count = out_pii.raw_entity_count if out_pii else 0
        total_raw_pii = in_raw_count + out_raw_count

        raw_types = {}
        for res in (in_pii, out_pii):
            if res:
                for t, c in res.raw_entities.items():
                    raw_types[t] = raw_types.get(t, 0) + c

        # Severity — max across input + output
        from deidentifier.policy_evaluator import Severity, _SEVERITY_RANK
        in_sev  = in_pii.severity  if in_pii  else Severity.NONE
        out_sev = out_pii.severity if out_pii else Severity.NONE
        max_sev = in_sev if _SEVERITY_RANK[in_sev] >= _SEVERITY_RANK[out_sev] else out_sev

        # Violations — flattened for metric_meta
        violations_meta = []
        for res, loc in ((in_pii, "input"), (out_pii, "output")):
            if res:
                for v in res.violations:
                    violations_meta.append({
                        "rule":         v.rule_name,
                        "severity":     v.severity.value,
                        "location":     loc,
                        "entity_types": sorted({e.entity_type for e in v.entities}),
                    })

        if in_count and out_count:
            pii_location = "both"
        elif in_count:
            pii_location = "input"
        elif out_count:
            pii_location = "output"
        else:
            pii_location = None

        # ---- Prompt injection (input only) ----
        pi_in = self._pi_cache.get(self._hash(input_text)) if input_text else None
        pi_score = float(pi_in["score"]) if pi_in else 0.0
        pi_ran   = bool(pi_in and pi_in.get("ran"))

        # ---- Moderation (input AND output, max across the two) ----
        mod_in  = self._mod_cache.get(self._hash(input_text))  if input_text  else None
        mod_out = self._mod_cache.get(self._hash(output_text)) if output_text else None

        harmful_in  = float(mod_in["harmful"])  if mod_in  else 0.0
        harmful_out = float(mod_out["harmful"]) if mod_out else 0.0

        harmful_score = max(harmful_in, harmful_out)

        mod_ran = bool((mod_in and mod_in.get("ran")) or (mod_out and mod_out.get("ran")))

        # Where did the highest score come from?
        mod_location = None
        if mod_in and (mod_in["harmful"] > 0):
            mod_location = "input"
        if mod_out and (mod_out["harmful"] > 0):
            mod_location = "both" if mod_location else "output"

        # ---- Thresholds (lazy — touches self.tox only if any toxicity step ran) ----
        # If no toxicity step ran for this span, the scores are 0.0 and the
        # default threshold is fine (verdict = 0 either way).
        if pi_ran or mod_ran:
            pi_threshold      = self.cfg.compass_toxicity_pi_threshold
            harmful_threshold = self.cfg.compass_toxicity_harmful_threshold
        else:
            pi_threshold = harmful_threshold = 0.50

        # ---- Decisions ----
        pi_detected  = 1.0 if pi_score      >= pi_threshold      else 0.0
        tox_detected = 1.0 if harmful_score >= harmful_threshold else 0.0

        # ---- Triggered models (for meta) ----
        triggered = []
        if pi_ran:
            triggered.append("prompt_injection")
        if mod_ran:
            triggered.append("moderation")

        return {
            # PII
            "pii_detected":   1.0 if total_pii > 0 else 0.0,
            "pii_count":      float(total_pii),
            "pii_types":      sorted(types.keys()),
            "pii_location":   pii_location,
            "pii_severity":   max_sev.value,
            "pii_raw_count":  int(total_raw_pii),
            "pii_raw_types":  sorted(raw_types.keys()),
            "pii_violations": violations_meta,
            # Prompt injection
            "prompt_injection_detected": pi_detected,
            "prompt_injection_score":    pi_score,
            "prompt_injection_ran":      pi_ran,
            # Toxicity (moderation)
            "toxicity_detected":     tox_detected,
            "harmful_content_score": harmful_score,
            "moderation_location":   mod_location,
            "moderation_ran":        mod_ran,
            # Meta
            "triggered_models": triggered,
        }