"""Safety lens — PII detection via Presidio.

Processes model_call spans only. PresidioEngine.process() runs ONCE in
build_context, producing three values that all three specs read from ctx:

    pii_count    — total PII entities detected (entities_processed)
    pii_detected — 1.0 / 0.0 binary flag
    pii_types    — value = count of distinct entity types,
                   metric_meta = JSON list e.g. '["EMAIL_ADDRESS","PERSON"]'
"""
from __future__ import annotations

import json
import logging
import os
import sys

from ..base import parse_meta, path_cols
from ..spec import MetricSpec, SpecWorker
from ..patterns import ctx_value, aggregation_derived
from ..predicates import llm_call

log = logging.getLogger("signal.worker.safety")

_PII_PKG = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                 "PHI-PII-De-Identification--model-v0")
)

LENS = "safety"


def _spec(metric, applies, pattern, inputs, unit, window="1h", threshold=False, per_span=True):
    return MetricSpec(metric=metric, lens=LENS, applies=applies, pattern=pattern,
                      inputs=inputs, unit=unit, window=window, threshold=threshold,
                      per_span=per_span)


SPECS = [
    _spec("pii_count",    llm_call, ctx_value("pii_count"),
          ["metadata.input → presidio.entities_processed"], "count", "1h", threshold=True),
    _spec("pii_detected", llm_call, ctx_value("pii_detected"),
          ["pii_count > 0"],                                "ratio", "1h", threshold=True),
    _spec("pii_types",    llm_call, ctx_value("pii_type_count"),
          ["distinct entity types detected"],               "count", "1h")
]


class SafetyWorker(SpecWorker):
    lens = LENS
    specs = SPECS
    span_types = ("model_call",)

    def __init__(self, cfg):
        super().__init__(cfg)
        self._pii_engine = None

    @property
    def pii_engine(self):
        if self._pii_engine is None:
            if _PII_PKG not in sys.path:
                sys.path.insert(0, _PII_PKG)
            from deidentifier.presidio.engine import PresidioEngine
            self._pii_engine = PresidioEngine.get_instance(
                spacy_model="en_core_web_sm",
                hf_model=None,
            )
            log.info("PresidioEngine loaded for SafetyWorker.")
        return self._pii_engine

    def build_context(self, span: dict) -> dict:
        """Run Presidio ONCE. All three specs read off this context."""
        md = parse_meta(span.get("metadata"))
        input_text = md.get("input") or ""

        if not input_text:
            return {
                "pii_count":      0.0,
                "pii_detected":   0.0,
                "pii_type_count": 0.0,
                "pii_types":      [],
            }

        result = self.pii_engine.process(input_text, document_id=span.get("span_id"))

        pii_types = list({e.entity_type for e in result.audit_record.entries})

        return {
            "pii_count":      float(result.audit_record.entities_processed),
            "pii_detected":   1.0 if result.audit_record.entities_processed > 0 else 0.0,
            "pii_type_count": float(len(pii_types)),
            "pii_types":      pii_types,
        }

    def compute(self, span: dict) -> list:
        """Same as SpecWorker.compute() — extended only to set metric_meta
        on the pii_types row with the actual entity type list."""
        if self.span_types and span.get("span_type") not in self.span_types:
            return []

        ctx = self.build_context(span)
        rows = []

        for scope in self.scopes_for(span):
            p = path_cols(span, scope)
            for spec in self.specs:
                if not spec.per_span:
                    continue
                if not spec.applies(span):
                    continue
                val = spec.pattern(span, ctx)
                if val is None:
                    continue
                row = self._row(p, span, spec.metric, val)
                if spec.metric == "pii_types" and ctx["pii_types"]:
                    row["metric_meta"] = json.dumps(ctx["pii_types"])
                rows.append(row)

        return rows
