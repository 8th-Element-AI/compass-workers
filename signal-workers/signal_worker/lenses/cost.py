"""Cost lens — declarative registry.

The 22 Cost metrics. Unlike Performance (where each span owns its value), cost is
ADDITIVE: monetary spend originates at the component leaf spans (model_call,
embedding, tool_call, retrieval). Solution / agent / workflow cost is obtained by
SUMMING those component costs at read time (the aggregated table's sum_value does
this) — the worker does NOT fabricate parent-scope cost per span.

The context-builder runs the pricing math ONCE per span: pull usage from the span
metadata, look up rates from the PricingCache (Postgres), compute the money. Every
spec is then a thin read off that context.

Token counts need no pricing; monetary metrics multiply usage by the cached rates.
"""
from __future__ import annotations

from ..base import parse_meta
from ..spec import MetricSpec, SpecWorker
from ..patterns import ctx_value, aggregation_derived
from ..predicates import (
    llm_call, billable, cost_embedding, cost_tool, cost_kb, sol_wf, solution_only,
)

LENS = "cost"


def _spec(metric, applies, pattern, inputs, unit, window="1d", threshold=False, per_span=True):
    return MetricSpec(metric=metric, lens=LENS, applies=applies, pattern=pattern,
                      inputs=inputs, unit=unit, window=window, threshold=threshold,
                      per_span=per_span)


SPECS = [
    # --- 3.1 Token consumption (counts; no pricing needed) ---
    _spec("input_tokens",     llm_call,       ctx_value("input_tokens"),     ["metadata.usage.input_tokens"],     "count", "1h"),
    _spec("output_tokens",    llm_call,       ctx_value("output_tokens"),    ["metadata.usage.output_tokens"],    "count", "1h"),
    _spec("total_tokens",     llm_call,       ctx_value("total_tokens"),     ["input_tokens", "output_tokens"],   "count", "1h", threshold=True),
    _spec("cached_tokens",    llm_call,       ctx_value("cached_tokens"),    ["metadata.usage.cached_tokens"],    "count", "1h"),
    _spec("reasoning_tokens", llm_call,       ctx_value("reasoning_tokens"), ["metadata.usage.reasoning_tokens"], "count", "1h"),
    _spec("embedding_tokens", cost_embedding, ctx_value("embedding_tokens"), ["metadata.embedding_tokens"],       "count", "1h"),
    _spec("search_units",     cost_kb,        ctx_value("search_units"),     ["metadata.search_units"],           "count", "1h"),

    # --- 3.2 Monetary cost (usage x rates from PricingCache) ---
    _spec("cost",                billable,       ctx_value("cost"),           ["usage", "pricing"],                "USD", "1d", threshold=True),
    _spec("input_cost",          llm_call,       ctx_value("input_cost"),     ["input_tokens", "pricing.input_per_1k"],   "USD", "1d"),
    _spec("output_cost",         llm_call,       ctx_value("output_cost"),    ["output_tokens", "pricing.output_per_1k"], "USD", "1d"),
    _spec("tool_api_cost",       cost_tool,      ctx_value("tool_api_cost"),  ["pricing.per_call"],                "USD", "1d"),
    _spec("embedding_cost",      cost_embedding, ctx_value("embedding_cost"), ["embedding_tokens", "pricing.input_per_1k"], "USD", "1d"),
    _spec("infrastructure_cost", sol_wf,         aggregation_derived(),       ["allocated infra"],                 "USD", "1d", per_span=False),

    # --- 3.3 Efficiency ---
    _spec("cost_per_outcome", sol_wf,   aggregation_derived(),    ["sum(cost)/count(outcomes)"],        "USD",       "1d", threshold=True, per_span=False),
    _spec("wasted_cost",      billable, ctx_value("wasted_cost"), ["cost", "span_status"],              "USD",       "1d"),
    _spec("retry_cost",       billable, ctx_value("retry_cost"),  ["cost", "metadata.retry_count"],     "USD",       "1d"),
    _spec("cost_per_token",   llm_call, ctx_value("cost_per_token"), ["cost", "total_tokens"],          "USD/token", "1h"),
    _spec("cache_savings",    llm_call, ctx_value("cache_savings"), ["cached_tokens", "pricing"],       "USD",       "1d"),
    _spec("cost_per_record",  sol_wf,   aggregation_derived(),    ["sum(cost)/count(records)"],         "USD",       "1d", per_span=False),

    # --- 3.4 Budget and forecasting (read-time over a window) ---
    _spec("budget_utilization", solution_only, aggregation_derived(), ["sum(cost)/budget"],        "0-1",    "1h", threshold=True, per_span=False),
    _spec("burn_rate",          sol_wf,        aggregation_derived(), ["sum(cost)/window_hours"],  "USD/hr", "1h", threshold=True, per_span=False),
    _spec("projected_cost",     solution_only, aggregation_derived(), ["burn_rate * horizon"],     "USD",    "1d", per_span=False),
]


class CostWorker(SpecWorker):
    lens = LENS
    specs = SPECS
    # cost originates only at billable component spans — read nothing else
    span_types = ("model_call", "embedding", "tool_call", "retrieval")

    def __init__(self, cfg, pricing=None):
        super().__init__(cfg)
        self._pricing = pricing

    @property
    def pricing(self):
        # built lazily so `CostWorker(None)` (e.g. show_specs) needs no DB
        if self._pricing is None:
            from ..pricing import PricingCache, PostgresPricingSource
            self._pricing = PricingCache(PostgresPricingSource(self.cfg.pg_dsn))
        return self._pricing

    def build_context(self, span: dict) -> dict:
        md = parse_meta(span.get("metadata"))
        usage = md.get("usage") if isinstance(md.get("usage"), dict) else {}
        st = span.get("span_type")
        cid = span.get("component_id") or ""
        # Only the billable component spans carry a price; everything else (solution,
        # workflow, agent spans) has cost 0 — don't even consult the cache for them.
        rates = self.pricing.rates(cid) if st in ("model_call", "embedding", "tool_call", "retrieval") else {}

        it = float(usage.get("input_tokens", 0) or 0)
        ot = float(usage.get("output_tokens", 0) or 0)
        rt = float(usage.get("reasoning_tokens", 0) or 0)
        ct = float(usage.get("cached_tokens", 0) or 0)
        emb = float(md.get("embedding_tokens", 0) or 0)
        su = float(md.get("search_units", 0) or 0)

        inp_rate = rates.get("input_per_1k", 0.0)
        out_rate = rates.get("output_per_1k", 0.0)
        cached_rate = rates.get("cached_input_per_1k", 0.0)
        per_call = rates.get("per_call", 0.0)
        per_query = rates.get("per_query", 0.0)

        input_cost = it / 1000.0 * inp_rate
        cache_cost = ct / 1000.0 * cached_rate
        output_cost = ot / 1000.0 * out_rate
        embedding_cost = emb / 1000.0 * inp_rate

        if st == "model_call":
            cost = input_cost + output_cost + cache_cost
        elif st == "embedding":
            cost = embedding_cost
        elif st == "tool_call":
            cost = per_call
        elif st == "retrieval":
            cost = per_query
        else:
            cost = 0.0

        total_tokens = it + ot
        status = (span.get("span_status") or "").lower()
        rc = float(md.get("retry_count", 0) or 0)

        return {
            "input_tokens": it, "output_tokens": ot, "reasoning_tokens": rt,
            "cached_tokens": ct, "total_tokens": total_tokens,
            "embedding_tokens": emb, "search_units": su,
            "input_cost": round(input_cost, 8), "output_cost": round(output_cost, 8),
            "embedding_cost": round(embedding_cost, 8),
            "tool_api_cost": round(per_call, 8) if st == "tool_call" else 0.0,
            "cost": round(cost, 8),
            "cost_per_token": round(cost / total_tokens, 10) if total_tokens else None,
            "cache_savings": round(ct / 1000.0 * max(0.0, inp_rate - cached_rate), 8),
            "wasted_cost": round(cost, 8) if status in ("error", "timeout") else 0.0,
            "retry_cost": round(cost * rc, 8),
        }
