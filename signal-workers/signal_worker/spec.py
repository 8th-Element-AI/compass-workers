"""Metric specs and the spec-driven worker engine.

A `MetricSpec` is pure declaration — name, which spans it applies to, which
compute pattern produces its value, what it reads, and rollup/threshold hints.
No logic lives in the spec itself.

`SpecWorker` is the engine: for each span it builds a per-span context once, then
walks the registered specs, runs the applicable ones, and emits derived rows. A
lens becomes a tiny subclass that sets `lens`, `specs`, and `build_context`.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable

from .base import BaseWorker, path_cols, DER_COLS


@dataclass
class MetricSpec:
    metric: str
    lens: str
    applies: Callable           # (span) -> bool
    pattern: Callable           # (span, ctx) -> float | None
    inputs: list = field(default_factory=list)
    unit: str = ""
    window: str = "5m"
    threshold: bool = False
    per_span: bool = True       # False => computed at read time, not by compute()


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
        return [span.get("scope") or "solution"]

    def owns(self) -> set:
        return {s.metric for s in self.specs}

    def _row(self, p: dict, span: dict, metric: str, value: float):
        return {
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
            "metric": metric,
            "value": float(value),
            "confidence": None,
            "metric_meta": None,
            "start_ts": span["started_at"],
            "end_ts": span["ended_at"],
        }

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
                rows.append(self._row(p, span, spec.metric, val))
        return rows
