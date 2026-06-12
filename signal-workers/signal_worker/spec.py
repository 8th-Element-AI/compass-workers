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
import logging
from dataclasses import dataclass, field
from collections import Counter
from typing import Callable, Optional

from .base import BaseWorker, path_cols
from .toggle_cache import ToggleCache

log = logging.getLogger("signal.worker")


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
    meta_fn: Optional[Callable] = None      # (span, ctx) -> dict | None


class SpecWorker(BaseWorker):
    specs: list = []

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self, cfg, toggle_cache: Optional[ToggleCache] = None):
        super().__init__(cfg)
        # Allow caller injection (used by offline --csv path to inject a
        # disabled cache so every span flows through unmodified).
        self._toggle_cache = toggle_cache
        # Per-batch skip stats — reset at the start of each process_batch
        self._batch_skipped_at_spec: Counter = Counter()

    @property
    def toggle_cache(self) -> ToggleCache:
        if self._toggle_cache is None:
            self._toggle_cache = ToggleCache(
                category=self.lens,
                pg_dsn=self.cfg.pg_dsn,
                ttl=self.cfg.signal_toggle_ttl,
            )
        return self._toggle_cache

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
    
    # ------------------------------------------------------------------
    # Gate helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _gate_key(scope: str, p: dict, metric: str):
        """Build the 7-tuple cache key matching the cache's storage layout."""
        return (
            scope,
            p["solution_id"],
            p["endpoint"],
            p["workflow_id"],
            p["agent_id"],
            p["component_id"],
            metric,
        )
    
    def _has_any_active_metrics(self, span: dict) -> bool:
        """Stage 1: any active metrics in this lens for this span's path?

        Iterates over (scope this span emits at) x (specs this lens owns).
        Stops on first hit. Stays O(specs * scopes) lookups, each O(1).
        """
        if self.span_types and span.get("span_type") not in self.span_types:
            return False
        active = self.toggle_cache.active
        for scope in self.scopes_for(span):
            p = path_cols(span, scope)
            for spec in self.specs:
                if not spec.per_span:
                    continue
                if not spec.applies(span):
                    continue
                if self._gate_key(scope, p, spec.metric) in active:
                    return True
        return False

    def filter_spans_by_gate(self, spans: list) -> list:
        """Keep only spans with at least one active metric for this lens.

        Public on purpose: Safety/Quality call this BEFORE expensive batch
        analysis so the expensive work isn't done for spans the gate will drop.
        """
        return [s for s in spans if self._has_any_active_metrics(s)]
    
    # ------------------------------------------------------------------
    # Row builder — invokes spec.meta_fn for per-row metric_meta
    # ------------------------------------------------------------------
    def _row(self, p: dict, span: dict, spec: MetricSpec, value: float, ctx: dict):
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
        if spec.meta_fn is not None:
            meta = spec.meta_fn(span, ctx)
            if meta:
                row["metric_meta"] = json.dumps(meta, separators=(",", ":"))
        return row

    # ------------------------------------------------------------------
    # compute() — per-span engine. Stage-2 spec-level gate lives here.
    # ------------------------------------------------------------------
    def compute(self, span: dict) -> list:
        if self.span_types and span.get("span_type") not in self.span_types:
            return []
        ctx = self.build_context(span)
        active = self.toggle_cache.active
        rows = []
        for scope in self.scopes_for(span):
            p = path_cols(span, scope)
            for spec in self.specs:
                if not spec.per_span:
                    continue
                if not spec.applies(span):
                    continue
                # Stage 2 — exact-match gate per (scope, path, metric)
                if self._gate_key(scope, p, spec.metric) not in active:
                    self._batch_skipped_at_spec[(scope, spec.metric)] += 1
                    continue
                val = spec.pattern(span, ctx)
                if val is None:
                    continue
                rows.append(self._row(p, span, spec, val, ctx))
        return rows

    # ------------------------------------------------------------------
    # process_batch() — applies stage-1 gate, then runs the shared engine
    # ------------------------------------------------------------------
    def process_batch(self, spans: list) -> list:
        original = len(spans)
        kept = self.filter_spans_by_gate(spans)
        return self._process_kept(original, kept, original - len(kept))

    def _process_kept(self, original: int, kept: list, skipped_at_gate: int) -> list:
        """Run compute() over already-gated spans and emit the per-batch summary.

        Single source of truth for the compute loop + logging. Lenses that
        need to do extra work between Stage 1 (gate filter) and the compute
        loop — e.g. Safety pre-filling its PII cache — call this directly
        instead of process_batch() to avoid double-filtering and double-logging.
        """
        self._batch_skipped_at_spec = Counter()

        rows = []
        for span in kept:
            rows.extend(self.compute(span))
        
        print(
            f"[{self.lens}] batch={original} processed={len(kept)} "
            f"skipped_at_gate={skipped_at_gate} emitted={len(rows)}"
        )
        
        if self._batch_skipped_at_spec:
            top = self._batch_skipped_at_spec.most_common(10)
            log.info(
                "[%s] skipped_at_spec (top %d): %s",
                self.lens, len(top), dict(top),
            )

        return rows

