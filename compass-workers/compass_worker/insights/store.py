"""RECONCILE + PERSIST — diff this tick's candidates against open state, write.

One Postgres transaction per tick:
  1. Load open insights / incidents (fingerprint → id).
  2. Upsert incidents; map each member candidate → its incident id.
  3. Upsert insights (INSERT new / UPDATE persisting), carrying incident_id.
  4. Resolve any open insight / incident whose fingerprint was NOT seen this tick.

Reconciliation key is the `fingerprint`; the partial unique indexes
(`... WHERE status='open'`) guarantee one open row per fingerprint, so an UPDATE
on the open row is always unambiguous.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger("compass.insights.store")


@dataclass
class PersistStats:
    opened: int = 0
    updated: int = 0
    resolved: int = 0
    incidents_open: int = 0
    incidents_resolved: int = 0


def _jsonb(d):
    return json.dumps(d, separators=(",", ":")) if d else None


def _load_open(cur, table: str) -> dict[str, str]:
    cur.execute(f"SELECT fingerprint, id::text FROM {table} WHERE status = 'open'")
    return {fp: iid for fp, iid in cur.fetchall()}


def _upsert_incidents(cur, incidents, open_incidents) -> tuple[dict[str, str], int]:
    """Insert/update incidents. Returns (candidate_fp → incident_id, open_count)."""
    cand_to_incident: dict[str, str] = {}
    for inc in incidents:
        if inc.fingerprint in open_incidents:
            iid = open_incidents[inc.fingerprint]
            cur.execute(
                """
                UPDATE incidents SET
                    confidence = %s, severity = %s::insight_severity_enum,
                    root_scope = %s::scope_level_enum, root_entity = %s,
                    member_count = %s, details = %s::jsonb, updated_at = now()
                WHERE id = %s::uuid
                """,
                (inc.confidence, inc.severity, inc.root_scope, inc.root_entity,
                 len(inc.members), _jsonb(inc.details), iid),
            )
        else:
            cur.execute(
                """
                INSERT INTO incidents
                    (fingerprint, solution_id, environment, confidence, severity,
                     status, root_scope, root_entity, member_count, details)
                VALUES (%s, %s, %s, %s, %s::insight_severity_enum,
                        'open'::insight_status_enum, %s::scope_level_enum, %s, %s, %s::jsonb)
                RETURNING id::text
                """,
                (inc.fingerprint, inc.solution_id, inc.environment, inc.confidence,
                 inc.severity, inc.root_scope, inc.root_entity, len(inc.members),
                 _jsonb(inc.details)),
            )
            iid = cur.fetchone()[0]
        for m in inc.members:
            cand_to_incident[m.fingerprint] = iid
    return cand_to_incident, len(incidents)


def _upsert_insight(cur, c, incident_id, open_insights) -> str:
    """INSERT new or UPDATE persisting. Returns 'opened' | 'updated'."""
    if c.fingerprint in open_insights:
        cur.execute(
            """
            UPDATE insights SET
                severity = %s::insight_severity_enum,
                observed_value = %s, threshold_value = %s,
                baseline_value = %s, deviation = %s,
                threshold_id = %s::uuid, details = %s::jsonb,
                incident_id = %s::uuid, last_seen = now(), updated_at = now()
            WHERE id = %s::uuid
            """,
            (c.severity, c.observed_value, c.threshold_value, c.baseline_value,
             c.deviation, c.threshold_id, _jsonb(c.details), incident_id,
             open_insights[c.fingerprint]),
        )
        return "updated"
    cur.execute(
        """
        INSERT INTO insights
            (fingerprint, lens, detection_mode, severity, status,
             scope, solution_id, endpoint, workflow_id, agent_id,
             component_id, component_type, environment,
             metric, time_window, operator,
             observed_value, threshold_value, baseline_value, deviation,
             threshold_id, details, incident_id)
        VALUES
            (%s, %s::threshold_category_enum, %s::insight_detection_mode_enum,
             %s::insight_severity_enum, 'open'::insight_status_enum,
             %s::scope_level_enum, %s, %s, %s, %s,
             %s, %s, %s,
             %s, %s, %s,
             %s, %s, %s, %s,
             %s::uuid, %s::jsonb, %s::uuid)
        """,
        (c.fingerprint, c.lens, c.detection_mode, c.severity,
         c.scope, c.solution_id, c.endpoint, c.workflow_id, c.agent_id,
         c.component_id, c.component_type, c.environment,
         c.metric, c.time_window, c.operator,
         c.observed_value, c.threshold_value, c.baseline_value, c.deviation,
         c.threshold_id, _jsonb(c.details), incident_id),
    )
    return "opened"


def persist(conn, candidates, incidents) -> PersistStats:
    """Reconcile + write everything for one tick. Single transaction."""
    stats = PersistStats()
    try:
        with conn.cursor() as cur:
            open_insights = _load_open(cur, "insights")
            open_incidents = _load_open(cur, "incidents")

            cand_to_incident, stats.incidents_open = _upsert_incidents(
                cur, incidents, open_incidents)

            seen_fps: list[str] = []
            for c in candidates:
                result = _upsert_insight(
                    cur, c, cand_to_incident.get(c.fingerprint), open_insights)
                seen_fps.append(c.fingerprint)
                if result == "opened":
                    stats.opened += 1
                else:
                    stats.updated += 1

            # resolve insights not seen this tick
            cur.execute(
                """
                UPDATE insights SET status='resolved', resolved_at=now(), updated_at=now()
                WHERE status='open' AND fingerprint <> ALL(%s::text[])
                """,
                (seen_fps,),
            )
            stats.resolved = cur.rowcount or 0

            # resolve incidents not seen this tick
            seen_inc = [i.fingerprint for i in incidents]
            cur.execute(
                """
                UPDATE incidents SET status='resolved', resolved_at=now(), updated_at=now()
                WHERE status='open' AND fingerprint <> ALL(%s::text[])
                """,
                (seen_inc,),
            )
            stats.incidents_resolved = cur.rowcount or 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats
