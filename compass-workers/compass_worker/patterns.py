"""Shared compute patterns.

A pattern is a factory that returns a function `(span, ctx) -> float | None`.
Returning None means "no value to emit" (e.g. the runtime never recorded the
attribute) — the engine skips it rather than fabricating a number.

The whole Performance lens runs on the six patterns below; later lenses add a
few more (pricing_cost, judge_dimension, token_usage, ...).
"""
from __future__ import annotations


def column_latency():
    """(ended_at - started_at) in milliseconds — from the span's own timing columns."""
    def f(span, ctx):
        return round(ctx["latency_ms"], 3)
    return f


def status_flag(match):
    """1.0 if span_status is in `match` else 0.0. Summed at rollup -> a rate/count."""
    match = set(match)
    def f(span, ctx):
        return 1.0 if ctx["status"] in match else 0.0
    return f


def metadata_numeric(key):
    """float(metadata[key]) when present, else None (not emitted)."""
    def f(span, ctx):
        v = ctx["md"].get(key)
        return None if v is None else float(v)
    return f


def metadata_bool(key):
    """1.0/0.0 from a metadata boolean when present, else None."""
    truthy = (True, 1, "1", "true", "True")
    def f(span, ctx):
        v = ctx["md"].get(key)
        if v is None:
            return None
        return 1.0 if v in truthy else 0.0
    return f


def ratio(num, den):
    """num(span,ctx) / den(span,ctx), guarded for missing numerator / zero denom."""
    def f(span, ctx):
        n = num(span, ctx)
        d = den(span, ctx)
        if n is None or not d:
            return None
        return round(float(n) / float(d), 4)
    return f


def aggregation_derived():
    """Marker pattern. Never produces a per-span value — the metric is computed at
    read time from the aggregated table (count / window, or a gauge). The spec is
    still registered so the metric is catalogued and thresholdable."""
    def f(span, ctx):
        return None
    return f


def ctx_value(field):
    """Read a value the context-builder already computed (e.g. a cost the pricing
    step produced). Returns None -> not emitted; returns 0.0 -> emitted as zero."""
    def f(span, ctx):
        return ctx.get(field)
    return f
