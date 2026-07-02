"""Column mapping: otel_traces → compass_raw_spans.

OTel spans carry a generic structure (SpanKind, SpanAttributes map, etc.).
Compass raw spans expect a richer, opinionated schema with dedicated columns
for solution_id, workflow_id, scope, etc.

Mapping priority for every Compass column:
  1. Explicit SpanAttribute with the 'compass.' namespace  (e.g. compass.scope)
  2. Plain SpanAttribute with the bare name               (e.g. scope)
  3. Heuristic derived from OTel fields                   (e.g. SpanKind → span_type)
  4. Empty string / None

Attributes consumed into dedicated columns are NOT repeated in the metadata blob.
Everything else from SpanAttributes lands in metadata as a JSON string.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

# Raw component_type values → canonical span_type for component-scoped spans.
# Handles common naming variants from different SDKs / instrumentations.
COMPONENT_TYPE_MAP: dict[str, str] = {
    # LLM / model inference
    "model":          "model_call",
    "model_call":     "model_call",
    "llm":            "model_call",
    "chat":           "model_call",
    "completion":     "model_call",
    "inference":      "model_call",
    # Retrieval / vector search
    "retrieval":      "retrieval",
    "retriever":      "retrieval",
    "vector_db":      "retrieval",
    "vectordb":       "retrieval",
    "vector_store":   "retrieval",
    "kb":             "retrieval",
    "knowledge_base": "retrieval",
    "search":         "retrieval",
    # Tool / function call
    "tool":           "tool_call",
    "tool_call":      "tool_call",
    "function":       "tool_call",
    "function_call":  "tool_call",
    "api":            "tool_call",
    "action":         "tool_call",
    # Embedding
    "embedding":      "embedding",
    "embed":          "embedding",
    "embedder":       "embedding",
    "embeddings":     "embedding",
    # Validation
    "validation":     "validation",
    "validator":      "validation",
    "validate":       "validation",
    # Skill / batch execution
    "skill_exec":     "skill_exec",
    "skill":          "skill_exec",
    "batch":          "skill_exec",
    "executor":       "skill_exec",
}

# OTel StatusCode → Compass span_status
STATUS_CODE_MAP: dict[str, str] = {
    "OK":    "ok",
    "ERROR": "error",
    "UNSET": "ok",
    "":      "ok",
}

# Column order must match the compass_raw_spans DDL exactly.
# partition_id is MATERIALIZED (computed by CH from trace_id) — omit from inserts.
RAW_COLS: list[str] = [
    "trace_id", "span_id", "parent_span_id", "correlation_id", "session_id",
    "span_type", "span_name", "span_status", "scope", "solution_id", "endpoint",
    "workflow_id", "agent_id", "component_id", "component_type",
    "started_at", "ended_at", "pipeline_stage", "stage_order", "entity_type",
    "service", "environment", "region", "metadata", "recorded_at",
]

# SpanAttribute keys that are lifted into dedicated columns.
# These are excluded from the metadata blob to avoid duplication.
_DEDICATED_KEYS: frozenset[str] = frozenset({
    "correlation_id", "session_id", "span_type", "scope",
    "solution_name", "endpoint_name", "workflow_name", "agent_name",
    "component_name", "component_type", "pipeline_stage", "stage_order",
    "entity_type", "environment", "region",
    # # compass-namespaced variants from instrumented SDKs
    # "compass.correlation_id", "compass.session_id", "compass.span_type",
    # "compass.scope", "compass.solution_id", "compass.endpoint",
    # "compass.workflow_id", "compass.agent_id", "compass.component_id",
    # "compass.component_type", "compass.pipeline_stage", "compass.stage_order",
    # "compass.entity_type",
})


def _pick(attrs: dict, *keys: str, default: str = "") -> str:
    """Return the first non-empty value found under any of keys, else default."""
    for k in keys:
        v = attrs.get(k)
        if v:
            return str(v)
    return default


def map_span(row: dict) -> dict:
    """Transform one otel_traces row into a compass_raw_spans row dict.

    Args:
        row: Column-name → value dict as returned by clickhouse-connect.
             Timestamp is a datetime object; Duration is an int (nanoseconds);
             SpanAttributes / ResourceAttributes are Python dicts.

    Returns:
        Dict keyed by RAW_COLS, ready for insertion into compass_raw_spans.
    """
    attrs: dict    = row.get("SpanAttributes")    or {}
    resource: dict = row.get("ResourceAttributes") or {}

    # ── Timestamps ────────────────────────────────────────────────────────────
    # clickhouse-connect returns DateTime64 columns as Python datetime objects.
    started_at: datetime = row["Timestamp"]
    duration_ns: int = row.get("Duration") or 0
    ended_at: datetime = started_at + timedelta(microseconds=duration_ns / 1_000)

    # ── span_status ───────────────────────────────────────────────────────────
    span_status = STATUS_CODE_MAP.get(
        (row.get("StatusCode") or "").upper(), "ok"
    )


    # ── scope ─────────────────────────────────────────────────────────────────
    # Fall back to 'endpoint' for root spans (no parent), 'component' otherwise.
    scope = _pick(attrs, "scope") or (
        "endpoint" if not (row.get("ParentSpanId") or "").strip() else "component"
    )

    # ── span_type ─────────────────────────────────────────────────────────────
    # endpoint/workflow/agent → span_type mirrors scope.
    # component → map component_type to canonical span_type via COMPONENT_TYPE_MAP.
    component_type = _pick(attrs, "component_type")
    if scope in ("endpoint", "workflow", "agent"):
        span_type = scope
    else:
        span_type = COMPONENT_TYPE_MAP.get(component_type.lower().strip(), component_type)

    # ── solution_id ───────────────────────────────────────────────────────────
    solution_id = (
        _pick(attrs, "solution_name")
        or (row.get("ServiceName") or "")
    )

    # ── environment ───────────────────────────────────────────────────────────
    environment = (
        _pick(attrs, "environment")
        or resource.get("deployment.environment", "")
    )

    # ── region ────────────────────────────────────────────────────────────────
    region = (
        _pick(attrs, "region")
        or resource.get("cloud.region", "")
        or resource.get("cloud.availability_zone", "")
    )

    # ── stage_order ───────────────────────────────────────────────────────────
    stage_order_raw = _pick(attrs, "stage_order")
    try:
        stage_order = int(stage_order_raw) if stage_order_raw else None
    except (ValueError, TypeError):
        stage_order = None

    # ── metadata ──────────────────────────────────────────────────────────────
    # All SpanAttributes not consumed into dedicated columns + StatusMessage + events.
    meta: dict = {k: v for k, v in attrs.items() if k not in _DEDICATED_KEYS}

    if row.get("StatusMessage"):
        meta["status_message"] = row["StatusMessage"]

    # Events — stored as parallel arrays (Nested type from ClickHouse).
    ev_timestamps = row.get("Events.Timestamp") or []
    ev_names      = row.get("Events.Name")      or []
    ev_attrs_list = row.get("Events.Attributes") or []
    if ev_names:
        meta["events"] = [
            {
                "timestamp": t.isoformat() if hasattr(t, "isoformat") else str(t),
                "name":       n,
                "attributes": a,
            }
            for t, n, a in zip(ev_timestamps, ev_names, ev_attrs_list)
        ]

    return {
        "trace_id":       row.get("TraceId")      or "",
        "span_id":        row.get("SpanId")        or "",
        "parent_span_id": row.get("ParentSpanId")  or "",
        "correlation_id": _pick(attrs, "correlation_id"),
        "session_id":     _pick(attrs, "session_id"),
        "span_type":      span_type,
        "span_name":      row.get("SpanName") or "",
        "span_status":    span_status,
        "scope":          scope,
        "solution_id":    solution_id,
        "endpoint":       _pick(attrs, "endpoint_name"),
        "workflow_id":    _pick(attrs, "workflow_name"),
        "agent_id":       _pick(attrs, "agent_name"),
        "component_id":   (
            _pick(attrs, "model_name", "gen_ai.request.model", "llm.model")
            or _pick(attrs, "component_name")
            if span_type == "model_call"
            else _pick(attrs, "component_name")
        ),
        "component_type": component_type,
        "started_at":     started_at,
        "ended_at":       ended_at,
        "pipeline_stage": _pick(attrs, "pipeline_stage"),
        "stage_order":    stage_order,
        "entity_type":    _pick(attrs, "entity_type"),
        "service":        row.get("ServiceName") or "",
        "environment":    environment,
        "region":         region,
        "metadata":       json.dumps(meta) if meta else "",
        "recorded_at":    datetime.now(timezone.utc),
    }
