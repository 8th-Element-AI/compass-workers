"""Safety lens — PII + toxicity (prompt injection, moderation).

Decomposed into four independently gated analysis steps:

  pii              — Presidio over input + output  -> pii_count, pii_detected
  toxicity_router  — FastText over input + output  -> routing decisions
  prompt_injection — DeBERTa PI over input         -> prompt_injection_detected
  moderation       — DeBERTa Mod over input+output -> toxicity_detected

Per-batch flow:

  process_batch(spans)
    -> Stage 1 gate (drop spans with no active safety toggle)
    -> for each step:
         spans_needing = sub-filter by step.metrics
         if empty: skip step entirely (its model NEVER loads)
         else: extract texts, dedup, step.analyze() in one batched call, cache
    -> for each kept span: build_context reads all 4 caches -> compute -> emit

Each step's model loads only the first time it's actually called with non-empty
input. If your PG thresholds only enable `pii_count`, the toxicity_router
step's `_spans_needing()` returns [] and FastText / both DeBERTas stay on
disk. Same idea for `prompt_injection_detected` vs `toxicity_detected` —
toggle one, the other model is untouched.
"""
from __future__ import annotations
 
import logging
import time
 
from ..base import parse_meta
from ..spec import MetricSpec, PrefillStep, SpecWorker
from ..utils import LRUCache
from ..patterns import ctx_value
from ..predicates import llm_call
 
log = logging.getLogger("signal.worker.safety")

LENS = "safety"

# These sets drive the toggle-gate sub-filtering (_spans_needing(kept, step.metrics)).
# They MUST match metric names in SPECS exactly. Adding a metric requires updating
# both the relevant set here and the SPECS list below.
PII_METRICS              = {"pii_detected"}
PROMPT_INJECTION_METRICS = {"prompt_injection_detected"}
MODERATION_METRICS       = {"toxicity_detected"}
TOXICITY_ALL_METRICS     = PROMPT_INJECTION_METRICS | MODERATION_METRICS

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
    """Always emit — proves the pipeline ran.

    Fields:
      prompt_injection_score : final score
      pi_bert_ran            : did PI BERT actually execute?
      pi_decision            : 'bert' | 'rule_short_circuit' | 'no_rules_data'
      pipeline_stage         : 'evaluated' iff rules ran on this text
      rule_reasons           : which deterministic rules fired (if any)
    """
    score        = float(ctx.get("prompt_injection_score", 0.0))
    pi_ran       = bool(ctx.get("prompt_injection_ran", False))
    pi_reason    = ctx.get("prompt_injection_reason", "no_data")
    rules_eval   = bool(ctx.get("rules_evaluated", False))

    meta = {
        "prompt_injection_score": round(score, 4),
        "pi_bert_ran":            pi_ran,
        "pi_decision":            pi_reason,
        "triggered_models":       ctx.get("triggered_models", []),
        "pipeline_stage":         "evaluated" if rules_eval else "skipped",
    }
    if ctx.get("rule_reasons"):
        meta["rule_reasons"] = ctx["rule_reasons"]
    return meta

def _toxicity_meta(span, ctx):
    """Always emit — proves the pipeline ran. Same shape pattern as _prompt_injection_meta."""
    harmful    = float(ctx.get("harmful_content_score", 0.0))
    mod_ran    = bool(ctx.get("moderation_ran", False))
    mod_reason = ctx.get("moderation_reason", "no_data")
    rules_eval = bool(ctx.get("rules_evaluated", False))

    meta = {
        "harmful_content_score": round(harmful, 4),
        "moderation_bert_ran":   mod_ran,
        "moderation_decision":   mod_reason,
        "triggered_models":      ctx.get("triggered_models", []),
        "pipeline_stage":        "evaluated" if rules_eval else "skipped",
    }
    if ctx.get("moderation_location"):
        meta["moderation_location"] = ctx["moderation_location"]
    if ctx.get("rule_reasons"):
        meta["rule_reasons"] = ctx["rule_reasons"]
    return meta


SPECS = [
    # PII
    _spec("pii_detected", llm_call, ctx_value("pii_detected"), ["pii_count > 0"], unit="ratio", window="1h", threshold=True, meta_fn=_pii_meta),

    # Prompt injection (input only)
    _spec("prompt_injection_detected", llm_call, ctx_value("prompt_injection_detected"), ["metadata.input -> fasttext + prompt_injection BERT"], unit="ratio", window="1h", threshold=True, meta_fn=_prompt_injection_meta),

    # Toxicity (harmful + sexual via moderation BERT, OR-aggregated, input + output)
    _spec("toxicity_detected", llm_call, ctx_value("toxicity_detected"), ["metadata.input + metadata.output -> fasttext + moderation BERT"], unit="ratio", window="1h", threshold=True, meta_fn=_toxicity_meta),
]

# ---- text extractors ----------------------------------------------------------

def _extract_input_and_output(span):
    """PII, toxicity_router, and moderation all use this.

    The router needs the union of what downstream steps want (input + output)
    so PI (input only) and Mod (input + output) both find routing data when
    they look up the router cache by text hash.
    """
    md = parse_meta(span.get("metadata"))
    return (md.get("input"), md.get("output"))

def _extract_input_only(span):
    """prompt_injection only — model output isn't a prompt-injection attack."""
    md = parse_meta(span.get("metadata"))
    return (md.get("input"),)


class SafetyWorker(SpecWorker):
    """Safety lens — PII + toxicity, four lazy-loaded analysis steps.

    See module docstring for the per-batch flow. The short story: each step
    runs only if at least one span in the batch has a matching active
    toggle, and the underlying model is constructed only on first call to
    that step's analyze() — so a worker that never sees an active
    `toxicity_detected` toggle never instantiates the moderation BERT.
    """
    lens = LENS
    specs = SPECS
    span_types = ("model_call",)

    def __init__(self, cfg, toggle_cache=None):
        super().__init__(cfg, toggle_cache=toggle_cache)

        # PII config
        self.pii_batch_size = cfg.signal_pii_batch_size
        self.ner_model = cfg.signal_pii_ner_model

        self.toxicity_batch_size = cfg.signal_toxicity_batch_size

        # Lazy holders. _tox is just a config + lazy model registry — its own
        # internal .fasttext/.prompt_injection/.moderation properties load
        # the actual weights on first access.
        self._pii_engine = None
        self._tox = None

        # Result caches — one per analyzer.
        self._pii_cache    = LRUCache(cfg.signal_pii_cache_max)
        self._router_cache = LRUCache(cfg.signal_toxicity_cache_max)
        self._pi_cache     = LRUCache(cfg.signal_toxicity_cache_max)
        self._mod_cache    = LRUCache(cfg.signal_toxicity_cache_max)


        self._steps = [
            PrefillStep(
                name="pii",
                metrics=PII_METRICS,
                cache=self._pii_cache,
                extract=_extract_input_and_output,
                analyze=self._analyze_pii,
            ),
            PrefillStep(
                name="toxicity_rules",
                metrics=TOXICITY_ALL_METRICS,
                cache=self._router_cache,
                extract=_extract_input_and_output,
                analyze=self._analyze_router,
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
        it touches no model weights; accessing .fasttext / .prompt_injection /
        .moderation is what loads them.

        Config comes from pydantic Settings (env-driven), not runtime.yaml.
        """
        if self._tox is None:
            from toxicity_observability import ToxicityClassifier
            cfg_dict = self._build_toxicity_config()
            log.info(
                "[safety] loading ToxicityClassifier (device=%s, models_root=%s)",
                cfg_dict["runtime"]["device"],
                self.cfg.signal_toxicity_models_root,
            )
            self._tox = ToxicityClassifier(config_dict=cfg_dict)
        return self._tox

    def _build_toxicity_config(self):
        """Project pydantic Settings into the dict shape ToxicityClassifier expects.

        Model paths: absolute paths pass through, relative ones are joined onto
        `signal_toxicity_models_root`. This way one env var (the root) parks the
        whole model tree in a container, and the four individual path fields
        rarely need to be touched.
        """
        from pathlib import Path
        c = self.cfg
        root = Path(c.signal_toxicity_models_root)

        def _resolve(rel: str) -> str:
            p = Path(rel)
            return str(p if p.is_absolute() else root / p)

        return {
            "models": {
                "prompt_injection":           {"local_path": _resolve(c.signal_toxicity_pi_path)},
                "prompt_injection_onnx_int8": {"local_path": _resolve(c.signal_toxicity_pi_onnx_path)},
                "moderation":                 {"local_path": _resolve(c.signal_toxicity_mod_path)},
            },
            "thresholds": {
                "prompt_injection_review": c.signal_toxicity_pi_threshold,
                "harmful_content_review":  c.signal_toxicity_harmful_threshold,
            },
            "runtime": {
                "device":            c.signal_toxicity_device,
                "max_length":        c.signal_toxicity_max_length,
                "fp16_on_cuda":      c.signal_toxicity_fp16,
                "full_scan_default": False,
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
 
    def _analyze_router(self, texts):
        """"Deterministic rules only — FastText removed. Cache entry holds the
        gate result PI/Mod will consult to decide short-circuit vs run BERT.

        Keeping the step name 'router' for log/cache continuity; semantically
        this is now 'rules + normalization'.
        """
        from toxicity_observability.normalize import normalize
        from toxicity_observability.deterministic import evaluate as deterministic_gate
        import toxicity_observability.constants as TC

        log.info("[safety:rules] evaluating %d unique texts", len(texts))

        out = []
        for t in texts:
            norm = normalize(t)
            rules = deterministic_gate(norm)
            out.append({
                "model_text":      norm.model_text,
                "rule_reasons":    rules.reasons,
                "pi_forced":       TC.PROMPT_INJECTION in rules.force_label,
                "harmful_forced":  TC.HARMFUL_CONTENT  in rules.force_label,
            })
        return out

    def _analyze_prompt_injection(self, texts):
        """PI BERT — only on texts FastText routed to attack and that didn't
        fast-allow. The model itself is loaded lazily on first call to
        self.tox.prompt_injection.classify_batch().
        """
        out = [None] * len(texts)
        bert_indices, bert_texts = [], []

        for i, t in enumerate(texts):
            r = self._router_cache.get(self._hash(t))
            if r is None:
                out[i] = {"score": 0.0, "ran": False, "reason": "no_rules_data"}
                continue
            if r["pi_forced"]:
                out[i] = {"score": 1.0, "ran": False, "reason": "rule_short_circuit"}
            else:
                bert_indices.append(i)
                bert_texts.append(r["model_text"])

        if bert_texts:
            log.info("[safety:pi] PI BERT over %d texts", len(bert_texts))
            results = self.tox.prompt_injection.classify_batch(
                bert_texts, batch_size=self.toxicity_batch_size,
            )
            for idx, res in zip(bert_indices, results):
                out[idx] = {
                    "score":  float(res["scores"]["prompt_injection"]),   # ← fixed
                    "ran":    True,
                    "reason": "bert",
                }
        return out

    def _analyze_moderation(self, texts):
        """Moderation BERT runs on every text that didn't trigger harmful/sexual
        rules. When a rule fires, the corresponding sub-label is set to 1.0
        directly and BERT is skipped.

        Note: if only ONE of {harmful, sexual} is forced, BERT is still skipped —
        we treat the rule as definitive for that text. Trade-off: we miss any
        BERT signal on the OTHER sub-label. Acceptable for observability since
        toxicity_detected = (harmful OR sexual) >= threshold either way.
        """
        out = [None] * len(texts)
        bert_indices, bert_texts = [], []

        for i, t in enumerate(texts):
            r = self._router_cache.get(self._hash(t))
            if r is None:
                out[i] = {"harmful": 0.0,"ran": False, "reason": "no_rules_data"}
                continue
            if r["harmful_forced"]:
                out[i] = {
                    "harmful": 1.0 if r["harmful_forced"] else 0.0,
                    "ran":     False,
                    "reason":  "rule_short_circuit",
                }
            else:
                bert_indices.append(i)
                bert_texts.append(r["model_text"])

        if bert_texts:
            log.info("[safety:mod] Moderation BERT over %d texts", len(bert_texts))
            results = self.tox.moderation.classify_batch(
                bert_texts, batch_size=self.toxicity_batch_size,
            )
            for idx, res in zip(bert_indices, results):
                out[idx] = {
                    "harmful": float(res["scores"]["harmful_content"]),
                    "ran":     True,
                    "reason":  "bert",
                }

        return out

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
        root = Path(c.signal_toxicity_models_root)

        def _resolve(rel: str) -> Path:
            p = Path(rel)
            return p if p.is_absolute() else root / p

        tox_artifacts = [
            ("prompt_injection",       _resolve(c.signal_toxicity_pi_path),        "dir",      True),
            ("prompt_injection_onnx",  _resolve(c.signal_toxicity_pi_onnx_path),   "dir",      False),  # CPU optimization, optional
            ("moderation",             _resolve(c.signal_toxicity_mod_path),       "dir",      True),
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

        # -- PII NER model in HF cache -------------------------------------
        if c.signal_pii_ner_model and "/" in c.signal_pii_ner_model:
            try:
                from huggingface_hub import try_to_load_from_cache
                cached = try_to_load_from_cache(c.signal_pii_ner_model, "config.json")
                if cached is None:
                    missing.append(("pii_ner_model (HF cache)", Path(c.signal_pii_ner_model)))
                    log.error("[safety] FAIL: PII NER model '%s' not in HF cache",
                            c.signal_pii_ner_model)
                else:
                    log.info("[safety]  OK: %-25s %s",
                            "pii_ner_model", c.signal_pii_ner_model)
            except ImportError:
                log.warning("[safety] huggingface_hub not installed — skipping PII NER cache check")

        # -- Bail loud if anything was missing -----------------------------
        if missing:
            lines = [
                f"Safety worker startup failed: {len(missing)} required artifact(s) not found:",
                *(f"  - {name}: {path}" for name, path in missing),
                "",
                "Common fixes:",
                f"  - Toxicity models: `toxicity-observe download` or set SIGNAL_TOXICITY_MODELS_ROOT correctly (current: {root})",
                f"  - PII NER: python -c \"from transformers import pipeline; "
                f"pipeline('ner', model='{c.signal_pii_ner_model}', aggregation_strategy='first')\"",
            ]
            raise FileNotFoundError("\n".join(lines))

        log.info("[safety] startup health check: PASSED")

    # ------------------------------------------------------------------
    # build_context — read all four caches, summarize per span
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

        # Severity — max across input + output (matches build_context's "any detection" semantics)
        from deidentifier.policy_evaluator import Severity, _SEVERITY_RANK
        in_sev  = in_pii.severity  if in_pii  else Severity.NONE
        out_sev = out_pii.severity if out_pii else Severity.NONE
        max_sev = in_sev if _SEVERITY_RANK[in_sev] >= _SEVERITY_RANK[out_sev] else out_sev

        # Violations — flattened for metric_meta (rule name + severity + entity types + which side)
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

        # ---- Router (just for meta — read by input text, the most common case) ----
        router_in = self._router_cache.get(self._hash(input_text)) if input_text else None

        # ---- Prompt injection (input only) ----
        pi_in = self._pi_cache.get(self._hash(input_text)) if input_text else None
        pi_score = float(pi_in["score"]) if pi_in else 0.0
        pi_ran   = bool(pi_in and pi_in.get("ran"))
        pi_reason = (pi_in.get("reason") if pi_in else "no_data")

        # ---- Moderation (input AND output, max per dimension) ----
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

        def _pick_reason(*reasons):
            priority = {"bert": 3, "rule_short_circuit": 2, "no_rules_data": 1, "no_data": 0}
            return max(reasons, key=lambda r: priority.get(r, 0))
        
        mod_reason = _pick_reason(
            mod_in.get("reason")  if mod_in  else "no_data",
            mod_out.get("reason") if mod_out else "no_data",
        )

        # ---- Thresholds (lazy — touches self.tox only if any toxicity metric ran) ----
        # If no toxicity step ran for this span, none of the toxicity ctx fields
        # are read and self.tox is never touched. But pi_score / harmful_score
        # are already 0.0 in that case, so toxicity_detected/pi_detected default
        # to 0.0 below without needing the config.
        if router_in is not None or pi_ran or mod_ran:
            pi_threshold      = self.cfg.signal_toxicity_pi_threshold
            harmful_threshold = self.cfg.signal_toxicity_harmful_threshold
        else:
            pi_threshold = harmful_threshold = 0.50  # any default works; all scores are 0.0

        # ---- Decisions ----
        pi_detected  = 1.0 if pi_score      >= pi_threshold       else 0.0
        tox_detected = 1.0 if (
            harmful_score >= harmful_threshold
        ) else 0.0

        # ---- Triggered models (for meta) ----
        triggered = []
        if router_in is not None:
            triggered.append("deterministic_rules")
        if pi_ran:
            triggered.append("prompt_injection")
        if mod_ran:
            triggered.append("moderation")

        return {
            # PII
            "pii_detected":   1.0 if total_pii > 0 else 0.0,
            "pii_count":      float(total_pii),         # still in ctx so _pii_meta can read it
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
            "prompt_injection_reason":   pi_reason,
            # Toxicity (moderation)
            "toxicity_detected":     tox_detected,
            "harmful_content_score": harmful_score,
            "moderation_location":   mod_location,
            "moderation_ran":        mod_ran,
            "moderation_reason":     mod_reason,
            # Router (for meta)
            "router_rule_reasons": (router_in.get("rule_reasons") if router_in else None),
            "rules_evaluated":  router_in is not None,
            "triggered_models": triggered,
        }
