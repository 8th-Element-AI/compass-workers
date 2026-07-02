"""SCAN — load the rulebook from Postgres.

The `thresholds` table stores the entity path as registry UUIDs, but
ClickHouse `compass_aggregated_metrics` carries human-readable string ids
(`sol_support`, `agt_triage`). So the load query JOINs thresholds to the
registry tables to resolve UUID → string id, exactly like ToggleCache does —
that way the rule's path matches what the engine will query in ClickHouse.

Only active rules are loaded (`is_active = true`). A threshold's warning/
critical bounds may be NULL (the reconciler seeds rows with NULL bounds and
is_active=false); a rule with both bounds NULL can still drive *drift*
detection (which uses a baseline, not the bounds) but contributes no
*threshold* breach.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("compass.insights.rules")


@dataclass
class Rule:
    """One active threshold row, with the entity path as string ids."""
    threshold_id: str
    lens: str               # threshold category == lens
    metric: str
    scope: str              # solution | endpoint | workflow | agent | component
    time_window: str        # e.g. "5m", "1h"
    operator: str           # "gt" | "lt"
    warning_value: float | None
    critical_value: float | None
    # entity path (string ids; "" where shallower than scope)
    solution_id: str
    endpoint: str
    workflow_id: str
    agent_id: str
    component_id: str
    component_type: str


_LOAD_SQL = """
    SELECT
        th.id::text,
        th.category::text,
        th.metric_name,
        th.scope::text,
        th.time_window,
        th.operator,
        th.warning_value,
        th.critical_value,
        s.solution_id                    AS sol_slug,
        -- ClickHouse stores the endpoint as the URL path (e.g. /api/v1/extract),
        -- not the registry slug (ep_extract_v1). Resolve to endpoints.path so the
        -- rule's path matches what the aggregated rows actually carry.
        COALESCE(e.path, '')             AS ep_slug,
        COALESCE(w.workflow_id, '')      AS wf_slug,
        COALESCE(a.agent_id, '')         AS ag_slug,
        COALESCE(c.component_id, '')     AS comp_slug,
        COALESCE(c.component_type::text, '') AS comp_type
    FROM thresholds th
    JOIN solutions  s ON s.id = th.solution_id
    LEFT JOIN endpoints  e ON e.id = th.endpoint_id
    LEFT JOIN workflows  w ON w.id = th.workflow_id
    LEFT JOIN agents     a ON a.id = th.agent_id
    LEFT JOIN components c ON c.id = th.component_id
    WHERE th.is_active = true
"""


def load_active_rules(conn) -> list[Rule]:
    """Return every active threshold rule with its path resolved to string ids."""
    with conn.cursor() as cur:
        cur.execute(_LOAD_SQL)
        rows = cur.fetchall()
    rules = [
        Rule(
            threshold_id=r[0],
            lens=r[1],
            metric=r[2],
            scope=r[3],
            time_window=r[4],
            operator=r[5],
            warning_value=r[6],
            critical_value=r[7],
            solution_id=r[8],
            endpoint=r[9],
            workflow_id=r[10],
            agent_id=r[11],
            component_id=r[12],
            component_type=r[13],
        )
        for r in rows
    ]
    log.info("[insights] loaded %d active rules", len(rules))
    return rules
