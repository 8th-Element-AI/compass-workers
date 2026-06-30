"""SCAN (ClickHouse half) — read the observed value + baseline per rule.

Reads `compass_aggregated_metrics`, merging the 1-minute base buckets up to the
rule's window. Results are grouped by `environment` so prod / staging / canary
produce independent signals.

v1 simplification: the observed value compared against the rule is the **mean**
(`avgMerge(avg_value)`). A future version can pick the statistic per metric
(e.g. p95 latency via `quantilesTDigestMerge`). p95/max are also returned in the
snapshot for context.

The path filter matches each path column exactly — including "" for levels
shallower than the rule's scope — which lines up with how the aggregated rows
store unfilled path levels (matches path_cols()).
"""
from __future__ import annotations

import logging

log = logging.getLogger("compass.insights.reader")

# Whitelisted window units → ClickHouse INTERVAL units. Anything else is rejected.
_UNITS = {
    "s": "SECOND", "m": "MINUTE", "h": "HOUR", "d": "DAY", "w": "WEEK",
}


def parse_window(window: str) -> tuple[int, str]:
    """'5m' -> (5, 'MINUTE'). Raises ValueError on anything unexpected.

    Returns a validated (number, unit) so the caller can build an INTERVAL
    string by interpolation safely (number is an int, unit is whitelisted).
    """
    w = (window or "").strip().lower()
    if not w or not w[-1].isalpha() or not w[:-1].isdigit():
        raise ValueError(f"bad time_window: {window!r}")
    n = int(w[:-1])
    unit = _UNITS.get(w[-1])
    if unit is None or n <= 0:
        raise ValueError(f"bad time_window: {window!r}")
    return n, unit


_DEFAULT_TABLE = "compass_aggregated_metrics"


def _read(ch, rule, ts_clause: str, table: str = _DEFAULT_TABLE) -> dict[str, dict]:
    """Run one grouped read for `rule` over the given ts predicate.

    Returns {environment: {"avg": float, "p95": float, "max": float, "n": int}}.
    `table` is the aggregated-metrics table name (configurable for the signal_*
    prefix / migrations); it is a trusted config value, not user input.
    """
    params = {
        "scope": rule.scope,
        "sol": rule.solution_id,
        "metric": rule.metric,
        "ep": rule.endpoint,
        "wf": rule.workflow_id,
        "ag": rule.agent_id,
        "comp": rule.component_id,
    }
    q = f"""
        SELECT
            environment,
            avgMerge(avg_value)                      AS avg,
            quantilesTDigestMerge(0.95)(quantiles)[1] AS p95,
            max(max_value)                           AS mx,
            sum(count)                               AS n
        FROM {table}
        WHERE scope = %(scope)s
          AND solution_id = %(sol)s
          AND metric = %(metric)s
          AND endpoint = %(ep)s
          AND workflow_id = %(wf)s
          AND agent_id = %(ag)s
          AND component_id = %(comp)s
          AND {ts_clause}
        GROUP BY environment
    """
    res = ch.query(q, parameters=params)
    out: dict[str, dict] = {}
    for env, avg, p95, mx, n in res.result_rows:
        out[env or ""] = {
            "avg": float(avg) if avg is not None else None,
            "p95": float(p95) if p95 is not None else None,
            "max": float(mx) if mx is not None else None,
            "n": int(n or 0),
        }
    return out


def read_current(ch, rule, table: str = _DEFAULT_TABLE) -> dict[str, dict]:
    """Observed value over the rule's window, per environment."""
    n, unit = parse_window(rule.time_window)
    return _read(ch, rule, f"ts >= now() - INTERVAL {n} {unit}", table)


def read_baseline(ch, rule, baseline_days: int, table: str = _DEFAULT_TABLE) -> dict[str, dict]:
    """Trailing baseline (now-Nd .. now-window), per environment."""
    n, unit = parse_window(rule.time_window)
    days = int(baseline_days)
    ts_clause = (
        f"ts >= now() - INTERVAL {days} DAY "
        f"AND ts < now() - INTERVAL {n} {unit}"
    )
    return _read(ch, rule, ts_clause, table)
