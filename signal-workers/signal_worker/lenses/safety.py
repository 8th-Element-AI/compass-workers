"""Safety lens — PII detection via Presidio.

Processes `model_call` spans. For each batch of spans we:

  1. Collect every input + output text (skipping empties).
  2. Run ONE batched Presidio call across them (`analyze_batch(texts, batch_size=4)`)
     — concurrent detection, identical texts deduped automatically.
  3. Stash the per-text results in an in-memory content-hash cache so a later
     batch hitting the same prompt (which is the common case) doesn't re-analyze.
  4. Per span, build_context looks up the cached results for *this span's*
     input + output, summarizes into pii fields, and the specs read off it.

No `compute()` override — per-row entity-type detail is attached via the
generic `meta_fn` hook on `MetricSpec`.
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

PII_METRICS       = {"pii_count", "pii_detected", "pii_distinct_types"}
TOXICITY_METRICS  = {"toxicity_score"}
INJECTION_METRICS = {"prompt_injection_detected", "jailbreak_attempt"}# future

def _spec(metric, applies, pattern, inputs, unit, window="1h",
          threshold=False, per_span=True, meta_fn=None):
    return MetricSpec(
        metric=metric, lens=LENS, applies=applies, pattern=pattern,
        inputs=inputs, unit=unit, window=window, threshold=threshold,
        per_span=per_span, meta_fn=meta_fn,
    )

# ---- meta_fn: attach entity types + which side the PII was on to each row ----
def _pii_meta(span, ctx):
    if not ctx.get("pii_types"):
        return None
    meta = {"types": ctx["pii_types"]}
    loc = ctx.get("pii_location")
    if loc:
        meta["location"] = loc                              # "input" | "output" | "both"
    return meta

def _toxicity_meta(span, ctx):
    if not ctx.get("toxicity_labels"):
        return None
    meta = {"labels": ctx["toxicity_labels"]}
    if ctx.get("prompt_injection_score"):
        meta["prompt_injection_score"] = ctx["prompt_injection_score"]
    if ctx.get("harmful_content_score"):
        meta["harmful_content_score"] = ctx["harmful_content_score"]
    if ctx.get("sexual_content_score"):
        meta["sexual_content_score"] = ctx["sexual_content_score"]
    if ctx.get("toxicity_triggered"):
        meta["triggered_models"] = ctx["toxicity_triggered"]
    if ctx.get("toxicity_skipped"):
        meta["skipped_models"] = ctx["toxicity_skipped"]
    if ctx.get("toxicity_routing"):
        meta["routing"] = ctx["toxicity_routing"]
    return meta


SPECS = [
    # ---- PII ----
    _spec("pii_count", llm_call, ctx_value("pii_count"),["metadata.input + metadata.output -> presidio.entity_count"], unit="count", window="1h", threshold=True, meta_fn=_pii_meta),
    _spec("pii_detected", llm_call, ctx_value("pii_detected"), ["pii_count > 0"], unit="ratio", window="1h", threshold=True),

    # ---- Content Safety ----
    _spec("toxicity_detected", llm_call, ctx_value("toxicity_detected"), ["metadata.input -> content_safety labels"], unit="ratio", window="1h", threshold=True, meta_fn=_toxicity_meta),
]

class SafetyWorker(SpecWorker):
    """Safety lens worker — runs Presidio across spans in batches.

    The PII module is imported as a normal package (`deidentifier`); install it
    in editable mode in the same environment (`pip install -e ./PII`). The
    `sys.path` hack from the earlier version is gone.

    Per-batch flow:
      process_batch(spans)
        -> collect (text, hash) pairs not already cached
        -> pii_engine.analyze_batch(unique_texts, batch_size=4)   # concurrent
        -> populate the LRU cache by hash
        -> for each span: super().compute(span) -> build_context reads cache
    """
    lens = LENS
    specs = SPECS
    span_types = ("model_call",)

    def __init__(self, cfg, toggle_cache=None):
        super().__init__(cfg, toggle_cache=toggle_cache)

        # Safety-specific config + state for the PII cache and engine.
        self.pii_batch_concurrency = cfg.signal_pii_batch
        self.ner_model = cfg.signal_pii_ner_model

        # Lazy analyzers
        self._pii_engine = None
        self._toxicity_classifier = None

        # Result caches (one per analyzer)
        self._pii_cache = LRUCache(cfg.signal_pii_cache_max)
        self._toxicity_cache = LRUCache(cfg.signal_toxicity_cache_max)

        # Step registry — the shared engine iterates these.
        self._steps = [
            PrefillStep(
                name="pii",
                metrics=PII_METRICS,
                cache=self._pii_cache,
                extract=_extract_input_and_output,
                analyze=self._analyze_pii,
            ),
            PrefillStep(
                name="toxicity",
                metrics=TOXICITY_METRICS,
                cache=self._toxicity_cache,
                extract=_extract_input_only,
                analyze=self._analyze_toxicity,
            ),
        ]
        
    # ---- lazy analyzers (each loaded only when first used) ----
    @property
    def pii_engine(self):
        if self._pii_engine is None:
            from deidentifier import PresidioEngine
            log.info("[safety] loading PresidioEngine (ner_model=%s)", self.ner_model)
            self._pii_engine = PresidioEngine.get_instance(ner_model=self.ner_model)
        return self._pii_engine
 
    @property
    def toxicity_classifier(self):
        if self._toxicity_classifier is None:
            from toxicity_observability import ToxicityClassifier
            log.info("[safety] loading ToxicityClassifier (config=%s)", self.toxicity_config)
            self._toxicity_classifier = ToxicityClassifier()
        return self._toxicity_classifier

    # ---- analyzer adapters (called by _prefill_cache) ----
    def _analyze_pii(self, texts):
        return self.pii_engine.analyze_batch(
            texts, batch_size=self.pii_batch_concurrency,
        )
 
    def _analyze_toxicity(self, texts):
        # Sequential: PyTorch DeBERTa moderation has thread-unsafe global
        # meta/fake tensor state. FastText handles 99%+ in ~1ms so serial
        # is fast enough. Returns None for individual failures so the
        # generic prefill skips caching those.
        out = []
        for t in texts:
            try:
                out.append(self._toxicity_classifier.classify(t))
            except Exception:
                log.exception("[safety] toxicity classify failed")
                out.append(None)
        return out
    
    # ------------------------------------------------------------------
    # process_batch: gate -> loop over steps -> shared engine
    # ------------------------------------------------------------------
    def process_batch(self, spans: list) -> list:
        t_start = time.time()
        original_count = len(spans)
 
        # Stage 1 — drop spans with no active safety threshold
        kept = self.filter_spans_by_gate(spans)
        skipped_at_gate = original_count - len(kept)
        if skipped_at_gate:
            log.info(
                "[safety] %d/%d spans skipped at gate before any analysis",
                skipped_at_gate, original_count,
            )
        if not kept:
            return []
 
        # Run each registered prefill — generic, time it uniformly.
        step_timings = []
        for step in self._steps:
            t = time.time()
            n_texts = self._prefill_cache(kept, step)
            step_timings.append((step.name, round((time.time() - t) * 1000, 1), n_texts))
 
        # Stage 2 (in compute) + emit, via shared engine
        t_emit = time.time()
        rows = self._process_kept(original_count, kept, skipped_at_gate)
        emit_ms = round((time.time() - t_emit) * 1000, 1)
 
        total_ms = round((time.time() - t_start) * 1000, 1)
        step_str = " | ".join(f"{n}={ms}ms ({k} texts)" for n, ms, k in step_timings)

        print(
            "[safety] latency | total=%.1fms | %s | emit=%.1fms | rows=%d"
            % (total_ms, step_str, emit_ms, len(rows))
        )

        log.info(
            "[safety] latency | total=%.1fms | %s | emit=%.1fms | rows=%d",
            total_ms, step_str, emit_ms, len(rows),
        )
        return rows
    
    # ------------------------------------------------------------------
    # build_context — reads both caches; compute() picks fields per spec
    # ------------------------------------------------------------------
    def build_context(self, span: dict) -> dict:
        md = parse_meta(span.get("metadata"))
        input_text = md.get("input") or ""
        output_text = md.get("output") or ""
 
        # PII (both sides)
        in_res = self._pii_cache.get(self._hash(input_text)) if input_text else None
        out_res = self._pii_cache.get(self._hash(output_text)) if output_text else None
 
        in_count = in_res.entity_count if in_res else 0
        out_count = out_res.entity_count if out_res else 0
        total = in_count + out_count
 
        types: dict[str, int] = {}
        for res in (in_res, out_res):
            if res:
                for t, c in res.entities.items():
                    types[t] = types.get(t, 0) + c
 
        if in_count and out_count:
            location = "both"
        elif in_count:
            location = "input"
        elif out_count:
            location = "output"
        else:
            location = None
 
        # Content safety (input only)
        toxicity_res = self._toxicity_cache.get(self._hash(input_text)) if input_text else None
        toxicity_scores = toxicity_res["scores"] if toxicity_res else {}
        toxicity_labels = toxicity_res["labels"] if toxicity_res else []
 
        return {
            # PII
            "pii_count":      float(total),
            "pii_detected":   1.0 if total > 0 else 0.0,
            "pii_type_count": float(len(types)),
            "pii_types":      sorted(types.keys()),
            "pii_location":   location,
            # Content safety
            "toxicity_detected":       1.0 if toxicity_labels else 0.0,
            "prompt_injection_score":  float(toxicity_scores.get("prompt_injection", 0.0)),
            "harmful_content_score":   float(toxicity_scores.get("harmful_content", 0.0)),
            "sexual_content_score":    float(toxicity_scores.get("sexual", 0.0)),
            "toxicity_labels":         toxicity_labels,
            "toxicity_routing":        toxicity_res["routing"]          if toxicity_res else None,
            "toxicity_triggered":      toxicity_res["triggered_models"] if toxicity_res else [],
            "toxicity_skipped":        toxicity_res["skipped_models"]   if toxicity_res else [],
        }
 
 
# ---- text extractors (module-level so multiple steps can share) ----
def _extract_input_and_output(span):
    md = parse_meta(span.get("metadata"))
    return (md.get("input"), md.get("output"))
 
 
def _extract_input_only(span):
    md = parse_meta(span.get("metadata"))
    return (md.get("input"),)