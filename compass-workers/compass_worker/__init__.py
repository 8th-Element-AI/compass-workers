"""Compass observability workers.

Workers read immutable spans from ClickHouse `compass_raw_spans`, compute
per-lens metrics, and append them to `compass_derived_metrics`. The materialized
view then rolls those into `compass_aggregated_metrics` automatically.

One worker per lens: performance, cost, safety (shipped); quality, outcomes
(planned). See ARCHITECTURE.md for the design.
"""
__version__ = "0.1.0"
