"""Metric catalog — what coverage cares about.

Owned at deploy time: maps every threshold-able metric across all lenses to
(required_span_types, applicable_scopes). Two consumers:

  * reconciler.py — at startup, upserts metric_catalog from this declaration,
    then uses it to seed thresholds rows for newly-discovered entities.
  * Phase 2 coverage views — join thresholds + observed_span_types against
    metric_catalog to compute dark / disabled / active per (entity, metric).

Adding a metric:
  1. Declare its spec in the lens file as today (predicate from predicates.py).
  2. If the predicate is new, add it to PREDICATE_INFO below.
  3. Re-deploy. The reconciler's startup catalog upsert picks it up; the
     thresholds seed pass on the next batch fills in the rows.
"""
from __future__ import annotations

import logging
from typing import Iterable

log = logging.getLogger("compass.catalog")


# ---- span_type -> deepest scope that span_type sits at ----------------------
_SCOPE_FOR_SPAN_TYPE = {
    "model_call":  "component",
    "embedding":   "component",
    "tool_call":   "component",
    "retrieval":   "component",
    "validation":  "component",
    "skill_exec":  "component",
    "agent":       "agent",
    "workflow":    "workflow",
    "solution":    "solution",
}
_SCOPE_ORDER = ["component", "agent", "workflow", "endpoint", "solution"]


def _scopes_at_or_above(span_types: Iterable[str]) -> list[str]:
    """Return scopes from the deepest required span_type up to solution.

    A component-level span_type (model_call etc.) makes the metric applicable
    at every scope from component up. A workflow-level span_type makes it
    applicable at workflow / endpoint / solution.

    '*' makes the metric applicable at every scope.
    """
    types = list(span_types)
    if "*" in types:
        return list(_SCOPE_ORDER)
    indices = [
        _SCOPE_ORDER.index(_SCOPE_FOR_SPAN_TYPE[st])
        for st in types
        if st in _SCOPE_FOR_SPAN_TYPE
    ]
    if not indices:
        return ["solution"]
    deepest_idx = min(indices)  # smaller index = deeper level
    return _SCOPE_ORDER[deepest_idx:]


# ---- Predicate registry -----------------------------------------------------
# predicate_name -> (required_span_types, applicable_scopes_override)
#
# When the override is None, applicable_scopes is computed from
# required_span_types via _scopes_at_or_above().
#
# Keep this in sync with compass_worker/predicates.py. Adding a metric that
# uses a predicate missing here raises at reconciler startup — a deploy-time
# failure, not a silent gap.
PREDICATE_INFO: dict[str, tuple[list[str], list[str] | None]] = {
    "any_span":       (["*"], None),
    "llm_call":       (["model_call"], None),
    "queued_op":      (["agent", "workflow"], None),
    "orchestrated":   (["workflow"], None),
    "retryable":      (["model_call", "tool_call", "retrieval", "embedding",
                        "validation", "agent"], None),
    "rate_limited":   (["model_call", "tool_call"], None),
    "batch_op":       (["validation", "skill_exec"], None),
    # Read-time gauges (throughput, concurrency). Thresholdable at any parent
    # of component, but not at component itself.
    "levels":         (["solution", "workflow", "agent"],
                       ["agent", "workflow", "endpoint", "solution"]),
    "sol_wf":         (["solution", "workflow"], None),
    "billable":       (["model_call", "embedding", "tool_call", "retrieval"], None),
    "cost_embedding": (["embedding"], None),
    "cost_tool":      (["tool_call"], None),
    "cost_kb":        (["retrieval"], None),
    "solution_only":  (["solution"], None),
    "retrieval_op":   (["retrieval"], None),
    "tool_op":        (["tool_call"], None),
    "validated_op":   (["validation"], None),
    "data_op":        (["validation", "skill_exec"], None),
    "output_bearing": (["model_call", "tool_call", "validation"], None),
    "schema_checked": (["model_call", "validation"], None),
}


def _lens_specs() -> list:
    """Lazy import every lens's SPECS so this module loads without lens-specific
    ML deps."""
    from .lenses.performance import SPECS as perf
    from .lenses.cost        import SPECS as cost
    from .lenses.safety      import SPECS as safety
    from .lenses.quality     import SPECS as quality
    return [*perf, *cost, *safety, *quality]


def build_catalog_rows() -> list[dict]:
    """One catalog row per threshold-able metric across all lenses.

    Raises:
        ValueError: duplicate metric name with conflicting shapes, or unregistered
            predicate. Both are deploy-time failures.
    """
    seen: dict[str, dict] = {}
    for spec in _lens_specs():
        if not spec.threshold:
            continue
        pname = spec.applies.__name__
        if pname not in PREDICATE_INFO:
            raise ValueError(
                f"metric={spec.metric!r} uses predicate {pname!r} which is not "
                f"registered in PREDICATE_INFO (compass_worker/catalog.py). "
                f"Add it before deploying."
            )
        required_types, scopes_override = PREDICATE_INFO[pname]
        scopes = scopes_override if scopes_override is not None \
                                 else _scopes_at_or_above(required_types)
        row = {
            "metric":              spec.metric,
            "lens":                spec.lens,
            "required_span_types": list(required_types),
            "applicable_scopes":   list(scopes),
            "inputs":              list(spec.inputs) if spec.inputs else [],
            "unit":                spec.unit,
            "default_window":      spec.window,
            "default_operator":    "gt",
        }
        prev = seen.get(spec.metric)
        if prev is not None and prev != row:
            raise ValueError(
                f"metric={spec.metric!r} declared with conflicting catalog rows "
                f"across lenses: {prev} vs {row}"
            )
        seen[spec.metric] = row
    return list(seen.values())


def upsert_metric_catalog(pg_conn) -> int:
    """Idempotently UPSERT every threshold-able metric into metric_catalog.

    Called once at reconciler startup. Safe across restarts — ON CONFLICT
    updates the columns in place.
    """
    rows = build_catalog_rows()
    if not rows:
        log.warning("metric_catalog: no threshold=True specs found across lenses")
        return 0

    with pg_conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO metric_catalog
                    (metric, lens, required_span_types, applicable_scopes,
                     inputs, unit, default_window, default_operator, updated_at)
                VALUES
                    (%(metric)s,
                     %(lens)s::threshold_category_enum,
                     %(required_span_types)s,
                     %(applicable_scopes)s::scope_level_enum[],
                     %(inputs)s,
                     %(unit)s,
                     %(default_window)s,
                     %(default_operator)s,
                     NOW())
                ON CONFLICT (metric) DO UPDATE SET
                    lens                = EXCLUDED.lens,
                    required_span_types = EXCLUDED.required_span_types,
                    applicable_scopes   = EXCLUDED.applicable_scopes,
                    inputs              = EXCLUDED.inputs,
                    unit                = EXCLUDED.unit,
                    default_window      = EXCLUDED.default_window,
                    default_operator    = EXCLUDED.default_operator,
                    updated_at          = NOW();
                """,
                row,
            )
    pg_conn.commit()
    log.info("metric_catalog: upserted %d rows", len(rows))
    return len(rows)


def load_metric_catalog(pg_conn) -> list[dict]:
    """Read the catalog back from PG for in-memory use by the reconciler.

    Casts enum columns to text / text[] so psycopg returns plain Python
    str / list[str]. Without the casts, psycopg returns enum arrays as a
    raw '{a,b,c}' string (no default adapter for custom enum arrays in
    psycopg3) — silently breaking any `for scope in scopes:` loop that
    expected a list.
    """
    with pg_conn.cursor() as cur:
        cur.execute("""
            SELECT metric,
                   lens::text                AS lens,
                   required_span_types,
                   applicable_scopes::text[] AS applicable_scopes,
                   default_window,
                   default_operator
              FROM metric_catalog
        """)
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Defensive: fail loudly if the adapter didn't give us a list. Catches
    # future regressions (e.g. psycopg version change, accidental cast removal).
    for r in rows:
        if not isinstance(r["applicable_scopes"], list):
            raise TypeError(
                f"metric_catalog.applicable_scopes came back as "
                f"{type(r['applicable_scopes']).__name__} for metric={r['metric']!r} "
                f"(value={r['applicable_scopes']!r}); expected list[str]. "
                f"This usually means the ::text[] cast was removed from the "
                f"SELECT — check load_metric_catalog() in catalog.py."
            )
    return rows