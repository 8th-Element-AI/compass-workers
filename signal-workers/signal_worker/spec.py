"""Metric specs and the spec-driven worker engine.

A `MetricSpec` is pure declaration — name, which spans it applies to, which
compute pattern produces its value, what it reads, and rollup/threshold hints.
No logic lives in the spec itself.

`SpecWorker` is the engine: for each span it builds a per-span context once, then
walks the registered specs, runs the applicable ones, and emits derived rows. A
lens becomes a tiny subclass that sets `lens`, `specs`, and `build_context`.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from .base import BaseWorker, path_cols, DER_COLS


@dataclass
class MetricSpec:
    metric: str
    lens: str
    applies: Callable                       # (span) -> bool
    pattern: Callable                       # (span, ctx) -> float | None
    inputs: list = field(default_factory=list)
    unit: str = ""
    window: str = "5m"
    threshold: bool = False
    per_span: bool = True                   # False => computed at read time, not by compute()
    # NEW: optional per-row metric_meta. Returns a dict (serialized to JSON in
    # the metric_meta column) or None. Used e.g. by the Safety lens to attach the
    # detected entity types + location to a pii_count row. Generic — any future
    # lens needing per-row evidence uses this hook instead of overriding compute.
    meta_fn: Optional[Callable] = None      # (span, ctx) -> dict | None


class SpecWorker(BaseWorker):
    specs: list = []

    # ---- a lens provides this: parse/derive everything the patterns need, ONCE ----
    def build_context(self, span: dict) -> dict:
        raise NotImplementedError

    # ---- which scope(s) a span emits at; root solution spans also mirror to endpoint ----
    def scopes_for(self, span: dict):
        level = span.get("scope") or span.get("span_type")
        if level == "solution":
            return ["solution", "endpoint"] if (span.get("endpoint") or "") else ["solution"]
        return [level]

    def owns(self) -> set:
        return {s.metric for s in self.specs}

    def _row(self, p: dict, span: dict, spec: "MetricSpec", value: float, ctx: dict):
        row = {
            "span_id": span["span_id"],
            "trace_id": span.get("trace_id", "") or "",
            "parent_span_id": span.get("parent_span_id", "") or "",
            "scope": p["scope"],
            "solution_id": p["solution_id"],
            "endpoint": p["endpoint"],
            "workflow_id": p["workflow_id"],
            "agent_id": p["agent_id"],
            "component_id": p["component_id"],
            "component_type": p["component_type"],
            "environment": p["environment"],
            "ts": span["ended_at"],
            "metric": spec.metric,
            "value": float(value),
            "confidence": None,
            "metric_meta": None,
            "start_ts": span["started_at"],
            "end_ts": span["ended_at"],
        }
        # generic per-row metadata hook (replaces per-lens compute() overrides)
        if spec.meta_fn is not None:
            meta = spec.meta_fn(span, ctx)
            if meta:
                row["metric_meta"] = json.dumps(meta, separators=(",", ":"))
        return row

    def compute(self, span: dict) -> list:
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
                if val is None:          # nothing to emit (e.g. attribute absent)
                    continue
                rows.append(self._row(p, span, spec, val, ctx))
        return rows

    # ---- batch hook: lenses with expensive per-text inference (Safety / Quality)
    # override this to run their model on a whole batch at once. Default just
    # loops compute() — cheap lenses (Performance, Cost) don't need to override.
    def process_batch(self, spans: list) -> list:
        rows = []
        for span in spans:
            rows.extend(self.compute(span))
        return rows
