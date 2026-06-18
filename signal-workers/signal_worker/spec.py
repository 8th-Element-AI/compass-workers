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
from typing import Callable, Optional, Set, List, Any, Iterable

from .base import BaseWorker, path_cols
from .toggle_cache import ToggleCache
from .utils import LRUCache

from .observability import SKIPPED_AT_GATE


log = logging.getLogger("signal.worker")


@dataclass
class MetricSpec:
    """Pure declaration of one metric — name, what it applies to, how it's
    computed, and rollup/threshold hints. No logic in the spec itself; the
    `applies` and `pattern` callables live in predicates.py / patterns.py.
    """
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


@dataclass
class PrefillStep:
    """One expensive batched analysis step (Presidio, content-safety, etc.).
 
    Hosted in spec.py because the pipeline is generic (sub-filter -> extract
    -> dedup -> analyze -> cache). Each lens defines its own steps in its
    __init__ and the shared engine iterates them.
    """
    name: str                               # log label: "pii", "content_safety"
    metrics: Set[str]                       # metric names that need this step
    cache: LRUCache                         # result cache populated here
    extract: Callable[[dict], Iterable[str]]  # span -> iterable of texts to analyze
    analyze: Callable[[List[str]], List[Optional[Any]]]
                                            # texts -> results (None = skip cache)

class SpecWorker(BaseWorker):
    """Engine that drives spec-declared metrics across spans.

    Per span: build_context() once, then walk self.specs, run applicable ones
    against the toggle gate, emit one derived row per (scope, metric) that
    survives the gate. A lens subclass needs only `lens`, `specs`, and
    `build_context()`.
    """
    specs: list = []

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self, cfg, toggle_cache: Optional[ToggleCache] = None):
        """Bind config and toggle cache.

        Args:
            cfg: Config from signal_worker.config.
            toggle_cache: Optional injected cache. If None, one is built lazily
                from cfg.pg_dsn. The offline --csv path injects a disabled cache
                so every span passes the gate.
        """
        super().__init__(cfg)
        self._toggle_cache = toggle_cache
        self._batch_skipped_at_spec: Counter = Counter()

    @property
    def toggle_cache(self) -> ToggleCache:
        """Lazy ToggleCache (built from cfg on first access)."""
        if self._toggle_cache is None:
            self._toggle_cache = ToggleCache(
                category=self.lens,
                pg_dsn=self.cfg.pg_dsn,
                ttl=self.cfg.signal_toggle_ttl,
            )
        return self._toggle_cache

    # ---- a lens provides this: parse/derive everything the patterns need, ONCE ----
    def build_context(self, span: dict) -> dict:
        """Lens-specific: parse the span once into a dict the patterns read.

        Subclasses must override. Whatever's expensive (metadata parse, latency
        math, pricing lookup, model inference cache lookup) goes here so each
        spec just reads off the result.
        """
        raise NotImplementedError

    # ---- which scope(s) a span emits at; root solution spans also mirror to endpoint ----
    def scopes_for(self, span: dict):
        """Return the scope(s) this span emits metrics at.

        Normally just the span's own scope. A root solution span also mirrors
        to 'endpoint' so endpoint-scoped dashboards have data.
        """
        level = span.get("scope") or span.get("span_type")
        if level == "solution":
            return ["solution", "endpoint"] if (span.get("endpoint") or "") else ["solution"]
        return [level]

    def owns(self) -> set:
        """Set of metric names this lens emits (from its declared specs)."""
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
    
    def _spans_needing(self, spans: list, metrics) -> list:
        """Filter `spans` down to those with an active threshold on at least
        one metric in `metrics` (a set/iterable of metric names).
 
        Used by lenses with multiple expensive batched analyses (Safety has
        PII + content-safety, Quality will have judge-call groups). Each
        prefill calls this with its own metric group so it skips spans that
        passed the main gate on a different metric group's threshold.
        """
        metric_set = set(metrics)
        active = self.toggle_cache.active
        result = []
        for span in spans:
            matched = False
            for scope in self.scopes_for(span):
                if matched:
                    break
                p = path_cols(span, scope)
                for metric in metric_set:
                    if self._gate_key(scope, p, metric) in active:
                        result.append(span)
                        matched = True
                        break
        return result
    
    @staticmethod
    def _hash(text: str) -> str:
        """16-hex-char content hash used as the cache key for prefill results."""
        import hashlib
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
 
    def _prefill_cache(self, kept: list, step: "PrefillStep") -> int:
        """Run one expensive batched analysis step and cache results by content hash.

        Pipeline:
          1. Sub-filter kept to spans whose threshold metrics overlap step.metrics.
          2. Extract texts via step.extract.
          3. Dedup against step.cache and within the batch.
          4. Call step.analyze on the unique remaining texts.
          5. Cache results by content hash for subsequent batches.

        Args:
            kept: Spans already past the Stage 1 gate.
            step: A PrefillStep describing what to extract and analyze.

        Returns:
            Number of texts actually sent to the analyzer (0 if all cached).
        """
        needed = self._spans_needing(kept, step.metrics)
        if not needed:
            log.info("[%s] %s: no spans need analysis", self.lens, step.name)
            return 0
 
        to_analyze: dict[str, str] = {}
        for span in needed:
            for text in step.extract(span):
                if not text:
                    continue
                h = self._hash(text)
                if step.cache.get(h) is None and h not in to_analyze:
                    to_analyze[h] = text
 
        if not to_analyze:
            log.info(
                "[%s] %s: %d spans need analysis, all texts cached",
                self.lens, step.name, len(needed),
            )
            return 0
 
        hashes = list(to_analyze.keys())
        texts = [to_analyze[h] for h in hashes]
        log.info(
            "[%s] %s: analyzing %d unique texts from %d spans",
            self.lens, step.name, len(texts), len(needed),
        )
        results = step.analyze(texts)
        for h, r in zip(hashes, results):
            if r is not None:
                step.cache.put(h, r)
        return len(texts)
    
    # ------------------------------------------------------------------
    # Row builder — invokes spec.meta_fn for per-row metric_meta
    # ------------------------------------------------------------------
    def _row(self, p: dict, span: dict, spec: MetricSpec, value: float, ctx: dict):
        """Build one derived-metric row from a (span, spec, value).

        Invokes spec.meta_fn (if set) to attach per-row metric_meta JSON.
        """
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
        """Run all applicable per-span specs for one span and return derived rows.

        Two gates are applied:
          - span_type filter (cheap reject when the lens has span_types set).
          - Stage 2 toggle gate (per (scope, path, metric)).

        Returns:
            Zero or more derived-row dicts (one per (scope, metric) that
            passed both gates and whose pattern returned a non-None value).
        """
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
        """Default batch path: apply Stage 1 gate, then run compute() per span.

        Args:
            spans: Raw span dicts.
        """
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

        slot_label = getattr(self, "_current_slot", "all")
        SKIPPED_AT_GATE.labels(lens=self.lens, slot=slot_label).inc(skipped_at_gate)

        self._batch_skipped_at_spec = Counter()

        rows = []
        for span in kept:
            rows.extend(self.compute(span))
        
        print(
            "[%s] batch=%d processed=%d skipped_at_gate=%d emitted=%d"
            % (self.lens, original, len(kept), skipped_at_gate, len(rows))
        )

        log.info(
            "[%s] batch=%d processed=%d skipped_at_gate=%d emitted=%d",
            self.lens, original, len(kept), skipped_at_gate, len(rows),
        )
        if self._batch_skipped_at_spec:
            top = self._batch_skipped_at_spec.most_common(10)
            log.info(
                "[%s] skipped_at_spec (top %d): %s",
                self.lens, len(top), dict(top),
            )
 
        return rows

