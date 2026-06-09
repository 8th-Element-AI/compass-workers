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

import hashlib
import logging
import os
import threading
from collections import OrderedDict

from ..base import parse_meta
from ..spec import MetricSpec, SpecWorker
from ..patterns import ctx_value
from ..predicates import llm_call

log = logging.getLogger("signal.worker.safety")

LENS = "safety"

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


SPECS = [
    _spec("pii_count",            llm_call, ctx_value("pii_count"),
          ["metadata.input + metadata.output -> presidio.entity_count"],
          unit="count", window="1h", threshold=True, meta_fn=_pii_meta),
    _spec("pii_detected",         llm_call, ctx_value("pii_detected"),
          ["pii_count > 0"], unit="ratio", window="1h", threshold=True),
    _spec("pii_distinct_types",   llm_call, ctx_value("pii_type_count"),
          ["distinct entity types detected across input + output"],
          unit="count", window="1h"),
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

    def __init__(self, cfg):
        super().__init__(cfg)

        self.cache_max = cfg.signal_pii_cache_max
        self.batch_concurrency = cfg.signal_pii_batch
        self.ner_model = cfg.signal_pii_ner_model

        self._pii_engine = None
        self._cache: "OrderedDict[str, object]" = OrderedDict()  # hash -> AnalysisResult
        self._cache_lock = threading.Lock()

    # ---- lazy PII engine (mirrors PricingCache property pattern) ----
    @property
    def pii_engine(self):
        if self._pii_engine is None:
            from deidentifier import PresidioEngine
            log.info("[safety] loading PresidioEngine (ner_model=%s)", self.ner_model)
            self._pii_engine = PresidioEngine.get_instance(ner_model=self.ner_model)
        return self._pii_engine

    # ---- LRU helpers ----
    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _cache_get(self, h: str):
        with self._cache_lock:
            if h in self._cache:
                self._cache.move_to_end(h)            # LRU touch
                return self._cache[h]
            return None

    def _cache_put(self, h: str, result):
        with self._cache_lock:
            self._cache[h] = result
            self._cache.move_to_end(h)
            while len(self._cache) > self.cache_max:
                self._cache.popitem(last=False)       # drop LRU

    # ---- batch hook: pre-fill the cache for an entire batch in ONE call ----
    def process_batch(self, spans: list) -> list:
        # 1. Collect texts that aren't already cached, deduped across the whole batch.
        to_analyze: dict[str, str] = {}                # hash -> text
        for span in spans:
            if span.get("span_type") not in self.span_types:
                continue
            md = parse_meta(span.get("metadata"))
            for text in (md.get("input"), md.get("output")):
                if not text:
                    continue
                h = self._hash(text)
                if self._cache_get(h) is None and h not in to_analyze:
                    to_analyze[h] = text

        # 2. One concurrent call covers everything we don't already have.
        if to_analyze:
            hashes = list(to_analyze.keys())
            texts = [to_analyze[h] for h in hashes]
            log.info("[safety] analyze_batch: %d unique texts (concurrency=%d)",
                     len(texts), self.batch_concurrency)
            results = self.pii_engine.analyze_batch(texts, batch_size=self.batch_concurrency)
            for h, r in zip(hashes, results):
                self._cache_put(h, r)

        # 3. Now defer to the standard per-span engine; build_context reads the cache.
        return super().process_batch(spans)

    # ---- build a per-span context from the cached input/output results ----
    def build_context(self, span: dict) -> dict:
        md = parse_meta(span.get("metadata"))
        input_text = md.get("input") or ""
        output_text = md.get("output") or ""

        in_res = self._cache_get(self._hash(input_text)) if input_text else None
        out_res = self._cache_get(self._hash(output_text)) if output_text else None

        in_count = in_res.entity_count if in_res else 0
        out_count = out_res.entity_count if out_res else 0
        total = in_count + out_count

        # Distinct types across input + output
        types: dict[str, int] = {}
        if in_res:
            for t, c in in_res.entities.items():
                types[t] = types.get(t, 0) + c
        if out_res:
            for t, c in out_res.entities.items():
                types[t] = types.get(t, 0) + c

        # Which side the PII was on
        if in_count and out_count:
            location = "both"
        elif in_count:
            location = "input"
        elif out_count:
            location = "output"
        else:
            location = None

        return {
            "pii_count":      float(total),
            "pii_detected":   1.0 if total > 0 else 0.0,
            "pii_type_count": float(len(types)),
            "pii_types":      sorted(types.keys()),
            "pii_location":   location,
        }