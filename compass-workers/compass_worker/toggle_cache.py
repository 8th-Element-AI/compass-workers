"""Metric Toggle cache for write-side gating.

Each lens worker loads only its own category's active metrics from
Postgres on first use, holds them as a flat `set` of 7-tuples for O(1)
exact-match lookup, and refreshes on a configurable TTL. Mirrors the
PricingCache pattern.

Cache key: (scope, solution_id, endpoint, workflow_id, agent_id,
           component_id, metric_name)

All elements are strings. Empty string ("") for fields the threshold
didn't fill in Postgres — matches the convention used by `path_cols()`
when blanking fields deeper than the threshold's scope.

The Postgres `thresholds` table stores PG UUIDs as FKs, but spans in
ClickHouse carry the human-readable string IDs (e.g. "sol_support",
"model_gpt4o"). The load query JOINs to convert UUID → string ID so
the cache key matches what the worker sees in span columns.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Set, Tuple

log = logging.getLogger("compass.toggle")

# 7-tuple key: (scope, solution_id, endpoint, workflow_id, agent_id,
#               component_id, metric_name). All strings; "" for unfilled.
ToggleKey = Tuple[str, str, str, str, str, str, str]


class ToggleCache:
    """In-process cache of active metrics for one lens category.

    Usage:
        cache = ToggleCache(category="safety", pg_dsn=..., ttl=300)
        if cache.is_active(scope, sol, ep, wf, agt, comp, metric):
            ...

    Or, more directly, since lookups are the hot path:
        active = cache.active           # set
        key = (scope, sol, ep, wf, agt, comp, metric)
        if key in active: ...

    Concurrency:
        First refresh is guarded by a lock so concurrent first-callers
        don't double-load. Subsequent reads of `.active` are lock-free
        (atomic single-reference swap on refresh).

    Failure mode:
        If Postgres is briefly unreachable on a refresh, the last known-
        good snapshot is served and a warning is logged. If we've never
        loaded, the exception is raised.

    Test / offline mode:
        Construct with `disabled=True` to make every lookup return True.
        Used by the --csv offline path where there is no Postgres.
    """

    _EMPTY: Set[ToggleKey] = set()

    class _AlwaysActive:
        """Sentinel set: every membership test returns True. Used when the
        cache is disabled (offline / --csv mode) so every span passes both
        gates without needing Postgres."""
        def __contains__(self, _key) -> bool:
            return True
        def __len__(self) -> int:
            return 0
        def __iter__(self):
            return iter(())

    _ALWAYS_ACTIVE = _AlwaysActive()

    def __init__(
        self,
        category: str,
        pg_dsn: str,
        ttl: float = 300.0,
        disabled: bool = False,
    ):
        self.category = category
        self.pg_dsn = pg_dsn
        self.ttl = ttl
        self.disabled = disabled
        self._set: Set[ToggleKey] = set()
        # Sentinel: 0.0 = never loaded. Anything > 0 = loaded at that wall time,
        # even if the load returned an empty set. DO NOT use `self._set` truthiness
        # to detect "loaded" — an empty set is falsy in Python, which would
        # collapse "loaded but no active rows" into "never loaded" and trigger
        # a PG round-trip on every gate check.
        self._loaded_at = 0.0
        self._refresh_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def active(self):
        """Return the current active-threshold set, refreshing if TTL expired.

        When disabled, returns an _AlwaysActive sentinel that responds True
        to every `key in active` check, so every span passes the gate without
        touching Postgres.
        """
        if self.disabled:
            return self._ALWAYS_ACTIVE
        self._refresh()
        return self._set

    def is_active(
        self,
        scope: str,
        solution_id: str,
        endpoint: str,
        workflow_id: str,
        agent_id: str,
        component_id: str,
        metric: str,
    ) -> bool:
        """Exact-match check for one (scope, path, metric) tuple."""
        if self.disabled:
            return True
        return (
            scope, solution_id, endpoint, workflow_id,
            agent_id, component_id, metric,
        ) in self.active

    def __contains__(self, key: ToggleKey) -> bool:
        return self.is_active(*key)

    def __len__(self) -> int:
        if self.disabled:
            return 0
        return len(self.active)

    def force_refresh(self) -> None:
        """Reload from Postgres now, ignoring TTL."""
        if self.disabled:
            return
        self._refresh(force=True)

    # ------------------------------------------------------------------
    # Internal — refresh + load
    # ------------------------------------------------------------------

    def _refresh(self, force: bool = False) -> None:
        now = time.time()
        # Fast path — loaded at least once AND within TTL. Uses _loaded_at
        # (not _set truthiness) so an empty result set still counts as loaded.
        if not force and self._loaded_at > 0.0 and (now - self._loaded_at) <= self.ttl:
            return
        with self._refresh_lock:
            # Re-check after acquiring lock — another thread may have just done it.
            if not force and self._loaded_at > 0.0 and (time.time() - self._loaded_at) <= self.ttl:
                return
            try:
                new_set = self._load_from_pg()
                self._set = new_set                       # atomic ref swap
                self._loaded_at = time.time()
                log.info(
                    "[toggles:%s] refreshed: %d active toggles",
                    self.category, len(new_set),
                )
            except Exception as e:
                # Never loaded — surface the error so callers know the cache
                # is unusable. Once loaded once, briefly unreachable PG means
                # we serve the stale snapshot and just log.
                if self._loaded_at == 0.0:
                    raise
                log.warning(
                    "[toggles:%s] refresh failed; serving stale snapshot: %s",
                    self.category, e,
                )

    def _load_from_pg(self) -> Set[ToggleKey]:
        import psycopg
        with psycopg.connect(self.pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.scope::text                   AS scope,
                    s.solution_id                   AS solution_id,
                    COALESCE(e.path, '')     AS endpoint,
                    COALESCE(w.workflow_id, '')     AS workflow_id,
                    COALESCE(a.agent_id, '')        AS agent_id,
                    COALESCE(c.component_id, '')    AS component_id,
                    t.metric_name                   AS metric
                FROM thresholds t
                JOIN      solutions  s ON t.solution_id  = s.id
                LEFT JOIN endpoints  e ON t.endpoint_id  = e.id
                LEFT JOIN workflows  w ON t.workflow_id  = w.id
                LEFT JOIN agents     a ON t.agent_id     = a.id
                LEFT JOIN components c ON t.component_id = c.id
                WHERE t.category::text = %s
                  AND t.is_active = TRUE
                """,
                (self.category,),
            )
            rows = cur.fetchall()
        return {tuple(r) for r in rows}