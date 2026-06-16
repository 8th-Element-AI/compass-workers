"""Quality lens — 11 P0 metrics: structural validation + data quality computed
mechanically from span metadata, semantic output/retrieval scores from local
models via the pluggable `QualityScorer` (see scorers.py).

Scope decisions (catalog p0 set):
  * drift_score             OUT — batch/baseline comparison, not a per-span metric
  * context_recall/precision OUT — need ground-truth relevance labels (eval data)
  * constraint_satisfaction  metadata-driven: computed only when the runtime
                             records a `metadata.constraints` object

Per-batch flow (mirrors the Safety lens):
  process_batch(spans)
    -> map trace_id -> model_call outputs            (chunk_utilization needs
       the answer a retrieval span's chunks fed into; best-effort within batch)
    -> collect uncached generation/retrieval scoring jobs, deduped by hash
    -> one batched scorer call per job kind; results into an LRU cache
    -> super().process_batch(spans): build_context reads the cache

Semantic scoring can be disabled (SIGNAL_QUALITY_SEMANTIC=0 -> mechanical
metrics only, no torch needed) and sampled (SIGNAL_QUALITY_SAMPLE=0.2 scores a
deterministic 20% of spans — stable across reruns, keyed on span_id).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict

from ..base import parse_meta
from ..spec import MetricSpec, SpecWorker
from ..patterns import ctx_value
from ..predicates import (
    any_span, llm_call, retrieval_op, tool_op, validated_op, data_op,
    output_bearing, schema_checked,
)

log = logging.getLogger("signal.worker.quality")

LENS = "quality"

SCHEMA_KEYS = ("expected_schema", "response_schema", "schema")
EMPTYISH = (None, "", [], {})


def _spec(metric, applies, pattern, inputs, unit, window="1h",
          threshold=False, per_span=True, meta_fn=None):
    return MetricSpec(metric=metric, lens=LENS, applies=applies, pattern=pattern,
                      inputs=inputs, unit=unit, window=window, threshold=threshold,
                      per_span=per_span, meta_fn=meta_fn)


# ---- meta_fns: audit evidence attached to the metric_meta column ----
def _gen_meta(span, ctx):
    return ctx.get("gen_meta")


def _ret_meta(span, ctx):
    return ctx.get("ret_meta")


def _tcv_meta(span, ctx):
    failed = ctx.get("tcv_failed")
    return {"failed": failed} if failed else None


def _accuracy_meta(span, ctx):
    errors = ctx.get("val_errors")
    return {"errors": errors[:20]} if errors else None


def _constraint_meta(span, ctx):
    meta = {}
    if ctx.get("violated"):
        meta["violated"] = ctx["violated"]
    if ctx.get("unchecked"):
        meta["unchecked"] = ctx["unchecked"]
    return meta or None


SPECS = [
    # --- 2.1 output scoring (local-model proxies; see scorers.py recipes) ---
    _spec("faithfulness",   llm_call, ctx_value("faithfulness"),
          ["metadata.input", "metadata.output -> NLI entailment"],
          unit="score", threshold=True, meta_fn=_gen_meta),
    _spec("coherence",      llm_call, ctx_value("coherence"),
          ["metadata.output -> adjacent-sentence NLI contradiction"],
          unit="score", threshold=True),
    _spec("completeness",   llm_call, ctx_value("completeness"),
          ["metadata.input + metadata.output -> embedding coverage"],
          unit="score", threshold=True),

    # --- 2.2 retrieval quality ---
    _spec("context_relevance", retrieval_op, ctx_value("context_relevance"),
          ["metadata.query + metadata.chunks[].text -> relevance cross-encoder"],
          unit="score", threshold=True, meta_fn=_ret_meta),
    _spec("chunk_utilization", retrieval_op, ctx_value("chunk_utilization"),
          ["metadata.chunks[].text vs same-trace model_call output"],
          unit="score"),

    # --- 2.3 data quality ---
    _spec("data_completeness", data_op, ctx_value("data_completeness"),
          ["validation: non-null fraction of metadata.input record",
           "skill_exec: records_processed / batch_size"],
          unit="score", threshold=True),
    _spec("data_accuracy",     validated_op, ctx_value("data_accuracy"),
          ["1 - len(metadata.output.errors) / fields"],
          unit="score", threshold=True, meta_fn=_accuracy_meta),

    # --- 2.4 structural validation (booleans -> avg = pass rate) ---
    _spec("schema_conformance", schema_checked, ctx_value("schema_conformance"),
          ["validation: metadata.valid",
           "model_call: declared schema's required keys present in output"],
          unit="ratio", threshold=True),
    _spec("format_correctness", output_bearing, ctx_value("format_correctness"),
          ["output parses as its declared/apparent format"],
          unit="ratio", threshold=True),
    _spec("constraint_satisfaction", any_span, ctx_value("constraint_satisfaction"),
          ["metadata.constraints checked against output (emitted only when declared)"],
          unit="ratio", meta_fn=_constraint_meta),
    _spec("tool_call_validity", tool_op, ctx_value("tool_call_validity"),
          ["metadata.tool, metadata.request, metadata.response, span_status"],
          unit="ratio", threshold=True, meta_fn=_tcv_meta),
]


# ---- mechanical helpers -----------------------------------------------------
def _as_obj(v):
    """Metadata payloads arrive as parsed objects (live) or JSON strings (CSV)."""
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return None


def _format_correctness(md, span_type):
    """1.0 / 0.0 when there's something checkable, None otherwise.
    Without a declared format the only verifiable claim is 'JSON-looking
    output must parse' — plain text has no format requirement to violate."""
    payload = md.get("response") if span_type == "tool_call" else md.get("output")
    if payload in EMPTYISH:
        return None
    if isinstance(payload, (dict, list)):
        return 1.0
    text = str(payload).strip()
    declared = str(md.get("output_format") or md.get("format") or "").lower()
    if declared == "json" or (not declared and text[:1] in "{["):
        return 1.0 if _as_obj(text) is not None else 0.0
    return 1.0


def _schema_conformance(md, span_type):
    if span_type == "validation":
        valid = md.get("valid")
        return None if valid is None else (1.0 if valid in (True, "true", "True", 1) else 0.0)
    # model_call: only checkable when the runtime declared an expected schema
    schema = next((_as_obj(md[k]) for k in SCHEMA_KEYS if md.get(k)), None)
    if not isinstance(schema, dict):
        return None
    out = _as_obj(md.get("output"))
    if not isinstance(out, dict):
        return 0.0
    required = schema.get("required") or list(schema.get("properties") or schema)
    return 1.0 if all(k in out for k in required) else 0.0


def _tool_call_validity(md, status):
    failed = []
    tool = md.get("tool")
    if not (isinstance(tool, str) and tool.strip()):
        failed.append("missing_tool_name")
    if not isinstance(_as_obj(md.get("request")), (dict, list)):
        failed.append("malformed_request")
    if status not in ("error", "timeout") and md.get("response") in EMPTYISH:
        failed.append("missing_response")
    return (0.0 if failed else 1.0), failed


def _data_completeness(md, span_type):
    if span_type == "validation":
        record = _as_obj(md.get("input"))
        if not isinstance(record, dict) or not record:
            return None
        filled = sum(1 for v in record.values() if v not in EMPTYISH)
        return round(filled / len(record), 4)
    # skill_exec: fraction of the batch's records actually processed
    processed, batch = md.get("records_processed"), md.get("batch_size")
    if processed is None or not batch:
        return None
    return round(min(1.0, float(processed) / float(batch)), 4)


def _data_accuracy(md):
    out = _as_obj(md.get("output"))
    if not isinstance(out, dict) or "errors" not in out:
        return None, None
    errors = out.get("errors") or []
    record = _as_obj(md.get("input"))
    fields = len(record) if isinstance(record, dict) and record else 1
    return round(max(0.0, 1.0 - len(errors) / fields), 4), errors


def _constraint_satisfaction(md):
    """Convention: metadata.constraints = {max_chars, min_chars, max_words,
    min_words, contains: [...], not_contains: [...], format: "json"}.
    Boolean: 1.0 only if every checkable constraint holds. Unknown constraint
    keys are reported as unchecked, not failed."""
    constraints = _as_obj(md.get("constraints"))
    if not isinstance(constraints, dict) or not constraints:
        return None, [], []
    out = md.get("output")
    text = out if isinstance(out, str) else json.dumps(out) if out is not None else ""
    violated, unchecked = [], []
    for key, want in constraints.items():
        try:
            if key == "max_chars":
                ok = len(text) <= float(want)
            elif key == "min_chars":
                ok = len(text) >= float(want)
            elif key == "max_words":
                ok = len(text.split()) <= float(want)
            elif key == "min_words":
                ok = len(text.split()) >= float(want)
            elif key == "contains":
                ok = all(s in text for s in (want if isinstance(want, list) else [want]))
            elif key == "not_contains":
                ok = not any(s in text for s in (want if isinstance(want, list) else [want]))
            elif key == "format":
                ok = _as_obj(text) is not None if str(want).lower() == "json" else None
            else:
                ok = None
        except (TypeError, ValueError):
            ok = None
        if ok is None:
            unchecked.append(key)
        elif not ok:
            violated.append(key)
    if len(unchecked) == len(constraints):
        return None, [], unchecked
    return (0.0 if violated else 1.0), violated, unchecked


class QualityWorker(SpecWorker):
    lens = LENS
    specs = SPECS
    span_types = ("model_call", "tool_call", "retrieval", "validation", "skill_exec")

    def __init__(self, cfg, scorer=None):
        super().__init__(cfg)
        # getattr-with-default so show_specs can instantiate with cfg=None
        self.semantic = bool(getattr(cfg, "signal_quality_semantic", True))
        self.sample = float(getattr(cfg, "signal_quality_sample", 1.0))
        self.cache_max = getattr(cfg, "signal_quality_cache_max", 20000)
        self._scorer = scorer
        self._cache: "OrderedDict[str, dict]" = OrderedDict()   # job key -> scores
        self._cache_lock = threading.Lock()

    # ---- lazy scorer (mirrors PricingCache / PresidioEngine pattern) ----
    @property
    def scorer(self):
        if self._scorer is None:
            from ..scorers import LocalScorer
            self._scorer = LocalScorer(
                nli_model=getattr(self.cfg, "signal_quality_nli_model",
                                  "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"),
                embed_model=getattr(self.cfg, "signal_quality_embed_model",
                                    "sentence-transformers/all-MiniLM-L6-v2"),
                relevance_model=getattr(self.cfg, "signal_quality_relevance_model",
                                        "cross-encoder/ms-marco-MiniLM-L-6-v2"),
                batch_size=getattr(self.cfg, "signal_quality_batch", 32),
            )
        return self._scorer

    # ---- LRU helpers (same shape as the Safety lens) ----
    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()[:16]

    def _cache_get(self, key: str):
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    def _cache_put(self, key: str, result: dict):
        with self._cache_lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max:
                self._cache.popitem(last=False)

    def _sampled(self, span: dict) -> bool:
        """Deterministic per-span sampling — stable across reruns."""
        if self.sample >= 1.0:
            return True
        if self.sample <= 0.0:
            return False
        h = int(hashlib.sha256(str(span.get("span_id")).encode()).hexdigest()[:8], 16)
        return (h % 10000) < self.sample * 10000

    # ---- job keys ----
    def _gen_key(self, md) -> str:
        return f"g:{self._hash(md.get('input'))}:{self._hash(md.get('output'))}"

    def _ret_key(self, query, chunk_texts, answer) -> str:
        return (f"r:{self._hash(query)}:"
                f"{self._hash('|'.join(chunk_texts))}:{self._hash(answer)}")

    @staticmethod
    def _chunk_texts(md) -> list:
        chunks = _as_obj(md.get("chunks")) or []
        return [c.get("text", "") for c in chunks if isinstance(c, dict)]

    # ---- batch hook: score everything uncached in one call per job kind ----
    def process_batch(self, spans: list) -> list:
        if self.semantic:
            self._score_batch(spans)
        return super().process_batch(spans)

    def _score_batch(self, spans: list):
        # answers per trace, for chunk_utilization (best-effort within the batch)
        trace_answers: dict = {}
        for span in spans:
            if span.get("span_type") != "model_call":
                continue
            out = parse_meta(span.get("metadata")).get("output")
            if out:
                tid = span.get("trace_id") or ""
                trace_answers[tid] = (trace_answers.get(tid, "") + "\n" + str(out)).strip()
        self._trace_answers = trace_answers

        gen_jobs: "OrderedDict[str, tuple]" = OrderedDict()
        ret_jobs: "OrderedDict[str, tuple]" = OrderedDict()
        for span in spans:
            if not self._sampled(span):
                continue
            st = span.get("span_type")
            md = parse_meta(span.get("metadata"))
            if st == "model_call" and (md.get("input") or md.get("output")):
                key = self._gen_key(md)
                if self._cache_get(key) is None and key not in gen_jobs:
                    gen_jobs[key] = (str(md.get("input") or ""), str(md.get("output") or ""))
            elif st == "retrieval":
                texts = self._chunk_texts(md)
                if not texts:
                    continue
                answer = trace_answers.get(span.get("trace_id") or "")
                key = self._ret_key(md.get("query") or "", texts, answer)
                if self._cache_get(key) is None and key not in ret_jobs:
                    ret_jobs[key] = (md.get("query") or "", texts, answer)

        if gen_jobs:
            log.info("[quality] scoring %d unique generation jobs", len(gen_jobs))
            for key, res in zip(gen_jobs, self.scorer.score_generation(list(gen_jobs.values()))):
                self._cache_put(key, res)
        if ret_jobs:
            log.info("[quality] scoring %d unique retrieval jobs", len(ret_jobs))
            for key, res in zip(ret_jobs, self.scorer.score_retrieval(list(ret_jobs.values()))):
                self._cache_put(key, res)

    # ---- per-span context: mechanical checks + cached semantic scores ----
    def build_context(self, span: dict) -> dict:
        st = span.get("span_type")
        md = parse_meta(span.get("metadata"))
        status = (span.get("span_status") or "").lower()
        ctx: dict = {}

        if st in ("model_call", "tool_call", "validation"):
            ctx["format_correctness"] = _format_correctness(md, st)
        if st in ("model_call", "validation"):
            ctx["schema_conformance"] = _schema_conformance(md, st)
        if st == "tool_call":
            ctx["tool_call_validity"], ctx["tcv_failed"] = _tool_call_validity(md, status)
        if st in ("validation", "skill_exec"):
            ctx["data_completeness"] = _data_completeness(md, st)
        if st == "validation":
            ctx["data_accuracy"], ctx["val_errors"] = _data_accuracy(md)
        cs, violated, unchecked = _constraint_satisfaction(md)
        ctx["constraint_satisfaction"] = cs
        ctx["violated"], ctx["unchecked"] = violated, unchecked

        if self.semantic:
            if st == "model_call":
                res = self._cache_get(self._gen_key(md))
                if res:
                    ctx.update(faithfulness=res["faithfulness"], coherence=res["coherence"],
                               completeness=res["completeness"], gen_meta=res.get("meta"))
            elif st == "retrieval":
                texts = self._chunk_texts(md)
                answer = getattr(self, "_trace_answers", {}).get(span.get("trace_id") or "")
                res = self._cache_get(self._ret_key(md.get("query") or "", texts, answer)) if texts else None
                if res:
                    ctx.update(context_relevance=res["context_relevance"],
                               chunk_utilization=res["chunk_utilization"],
                               ret_meta=res.get("meta"))
        return ctx
