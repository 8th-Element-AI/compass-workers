"""Insights Engine — derive prioritized insights + incidents from metrics.

Interval-driven worker that reads aggregated metrics (ClickHouse) and rules
(Postgres), detects threshold breaches and baseline drift, groups correlated
signals into incidents, and writes them to Postgres. See docs/insights.md.
"""
from .worker import InsightsWorker

__all__ = ["InsightsWorker"]
