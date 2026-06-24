"""Performance lens — declarative registry.

The 17 Performance metrics, each declared as a MetricSpec over the shared
predicates (applies-to) and patterns (compute logic). The worker is now just
SpecWorker + a per-span context builder; all the logic lives in patterns and
all the routing in predicates.

Categorical fields (error_type/code/message/severity/source, http_status_code,
degradation_level) are NOT specs - they have no numeric value. They ride along
in the span metadata and are read as grouping context, never emitted as rows.
"""
from __future__ import annotations

from ..base import to_dt, parse_meta
from ..spec import MetricSpec, SpecWorker
from ..patterns import (
    column_latency, status_flag, metadata_numeric, metadata_bool, ratio,
    aggregation_derived, ctx_value,
)
from ..predicates import (
    any_span, llm_call, queued_op, orchestrated, retryable, rate_limited,
    batch_op, levels, sol_wf,
)

LENS = "performance"


def _spec(metric, applies, pattern, inputs, unit, window="5m", threshold=False, per_span=True):
    return MetricSpec(metric=metric, lens=LENS, applies=applies, pattern=pattern,
                      inputs=inputs, unit=unit, window=window, threshold=threshold,
                      per_span=per_span)


SPECS = [
    # --- column-derivable: always emitted, no instrumentation required ---
    _spec("latency",        any_span, column_latency(),            ["started_at", "ended_at"], "ms",   "5m", threshold=True),
    _spec("error_rate",     any_span, status_flag({"error", "timeout"}), ["span_status"],       "ratio","5m", threshold=True),
    _spec("timeout_count",  any_span, status_flag({"timeout"}),    ["span_status"],            "count","5m"),

    # --- extracted from metadata when the runtime recorded the attribute ---
    _spec("time_to_first_token", llm_call,     ctx_value("ttft_ms"),                      ["metadata.first_token_at", "started_at"],                   "ms", "5m", threshold=True),
#    _spec("inter_token_latency", llm_call,     metadata_numeric("inter_token_latency_ms"),["metadata.inter_token_latency_ms"],"ms", "5m"),
    _spec("queue_wait_time",     queued_op,    ctx_value("queue_wait_ms"),                ["metadata.enqueued_at", "metadata.scheduled_at"],           "ms", "5m"),
    _spec("scheduling_delay",    orchestrated, ctx_value("scheduling_delay_ms"),          ["metadata.scheduled_at", "started_at"],                     "ms", "5m"),
    _spec("retry_count",         retryable,    metadata_numeric("retry_count"),           ["metadata.retry_count"],           "count","15m", threshold=True),
    _spec("retry_delay",         retryable,    metadata_numeric("retry_delay_ms"),        ["metadata.retry_delay_ms"],        "ms", "15m"),
    _spec("rate_limit_hit",      rate_limited, metadata_bool("rate_limit_hit"),           ["metadata.rate_limit_hit"],        "ratio","5m"),
    _spec("rate_limit_wait",     rate_limited, metadata_numeric("rate_limit_wait_ms"),    ["metadata.rate_limit_wait_ms"],    "ms", "5m"),
    _spec("records_processed",   batch_op,     metadata_numeric("records_processed"),     ["metadata.records_processed"],     "count","5m"),
#    _spec("batch_size",          batch_op,     metadata_numeric("batch_size"),            ["metadata.batch_size"],            "count","5m"),

    # --- per-span ratio over token usage (shares the Cost context's usage block) ---
    _spec("token_throughput",    llm_call,
          ratio(lambda s, c: c["usage"].get("output_tokens"), lambda s, c: c["latency_s"]),
          ["metadata.usage.output_tokens", "latency"], "tok/sec", "5m"),

    # --- aggregation-derived: catalogued + thresholdable, computed at read time ---
    _spec("throughput",         levels, aggregation_derived(), ["count(spans)"],         "ops/sec", "5m", threshold=True, per_span=False),
    _spec("concurrency",        levels, aggregation_derived(), ["overlapping spans"],    "gauge",   "5m", per_span=False),
#    _spec("messages_in_flight", sol_wf, aggregation_derived(), ["queue gauge"],          "gauge",   "5m", per_span=False),
]


class PerformanceWorker(SpecWorker):
    lens = LENS
    specs = SPECS

    def build_context(self, span: dict) -> dict:
        started = to_dt(span["started_at"])
        ended = to_dt(span["ended_at"])
        latency_ms = (ended - started).total_seconds() * 1000.0
        md = parse_meta(span.get("metadata"))
        usage = md.get("usage") if isinstance(md.get("usage"), dict) else {}

        # ttft_ms: first_token_at (metadata) − started_at
        ft_raw = md.get("first_token_at")
        ttft_ms = (to_dt(ft_raw) - started).total_seconds() * 1000.0 if ft_raw else None

        # queue_wait_ms: scheduled_at − enqueued_at  (both in metadata)
        enq_raw  = md.get("enqueued_at")
        sched_raw = md.get("scheduled_at")
        queue_wait_ms = (
            (to_dt(sched_raw) - to_dt(enq_raw)).total_seconds() * 1000.0
            if enq_raw and sched_raw else None
        )

        # scheduling_delay_ms: started_at − scheduled_at
        scheduling_delay_ms = (
            (started - to_dt(sched_raw)).total_seconds() * 1000.0
            if sched_raw else None
        )

        return {
            "started": started,
            "ended": ended,
            "latency_ms": latency_ms,
            "latency_s": latency_ms / 1000.0,
            "status": (span.get("span_status") or "").lower(),
            "md": md,
            "usage": usage,
            "ttft_ms": ttft_ms,
            "queue_wait_ms": queue_wait_ms,
            "scheduling_delay_ms": scheduling_delay_ms,
        }
