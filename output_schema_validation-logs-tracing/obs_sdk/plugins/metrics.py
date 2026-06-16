"""
MetricsPlugin — application metrics via OTel Metrics SDK.

Exports metrics through the OTel Collector to Prometheus.
Provides standard instruments for request counts, latency,
LLM token usage, cost tracking, and error counts.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from obs_sdk.config import ObsConfig
from obs_sdk import context as ctx
from obs_sdk.plugins.base import ObsPlugin, obs_plugin

try:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    _OTEL_METRICS_AVAILABLE = True
except ImportError:
    _OTEL_METRICS_AVAILABLE = False


@obs_plugin("metrics")
class MetricsPlugin(ObsPlugin):
    """Application metrics via OTel Metrics SDK → Collector → Prometheus."""

    @property
    def name(self) -> str:
        return "metrics"

    def initialize(self, config: ObsConfig, **dependencies: Any) -> None:
        """Set up the OTel metrics pipeline.

        Creates: MeterProvider → PeriodicExportingMetricReader (flushes every 30s)
        → OTLPMetricExporter → OTel Collector → Prometheus.

        Pre-creates 5 instruments:
        - requests_total (counter): general request counting
        - llm_tokens_total (counter): input/output token tracking per model
        - llm_cost_usd_total (counter): running cost per model
        - errors_total (counter): error counting per model/operation
        - request_latency_ms (histogram): latency distribution

        Reuses the Resource from TracingPlugin so service_name is consistent.
        """
        self._config = config
        self._provider: Any = None
        self._meter: Any = None
        self._counters: dict = {}
        self._histograms: dict = {}

        if not config.metrics_enabled:
            return

        if not _OTEL_METRICS_AVAILABLE:
            print(
                "[obs-sdk] opentelemetry metrics packages not installed — "
                "metrics disabled.",
                file=sys.stderr,
            )
            return

        try:
            # Reuse the Resource from the tracing plugin if available
            resource = None
            tracing = dependencies.get("tracing")
            if tracing and hasattr(tracing, "resource") and tracing.resource:
                resource = tracing.resource

            if resource is None:
                from opentelemetry.sdk.resources import Resource
                resource = Resource.create({
                    "service.name": config.service_name,
                    "deployment.environment": config.environment,
                })

            exporter = OTLPMetricExporter(
                endpoint=f"{config.otel_collector_endpoint}/v1/metrics"
            )
            reader = PeriodicExportingMetricReader(
                exporter, export_interval_millis=30_000
            )
            self._provider = MeterProvider(
                resource=resource, metric_readers=[reader]
            )
            self._meter = self._provider.get_meter("obs_sdk")

            # Standard instruments
            self._counters["requests_total"] = self._meter.create_counter(
                "requests_total",
                description="Total request count",
            )
            self._histograms["request_latency_ms"] = self._meter.create_histogram(
                "request_latency_ms",
                description="Request latency in milliseconds",
                unit="ms",
            )
            self._counters["llm_tokens_total"] = self._meter.create_counter(
                "llm_tokens_total",
                description="LLM token usage",
            )
            self._counters["llm_cost_usd_total"] = self._meter.create_counter(
                "llm_cost_usd_total",
                description="LLM cost in USD",
            )
            self._counters["errors_total"] = self._meter.create_counter(
                "errors_total",
                description="Total error count",
            )
        except Exception as exc:
            print(f"[obs-sdk] Metrics init failed: {exc}", file=sys.stderr)
            self._provider = None
            self._meter = None

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _enriched_attributes(attributes: dict) -> dict:
        """Merge correlation/entity context from ContextVars into attributes."""
        enriched = dict(attributes)
        correlation_id = ctx.get_correlation_id()
        if correlation_id is not None:
            enriched.setdefault("correlation_id", correlation_id)
        entity = ctx.get_entity()
        if entity.get("entity_id") is not None:
            enriched.setdefault("entity_id", entity["entity_id"])
        if entity.get("entity_type") is not None:
            enriched.setdefault("entity_type", entity["entity_type"])
        if entity.get("entity_version") is not None:
            enriched.setdefault("entity_version", entity["entity_version"])
        return enriched

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def increment(
        self,
        name: str,
        value: int = 1,
        **attributes: Any,
    ) -> None:
        """
        Increment a counter by *value*.

        Parameters
        ----------
        name:
            Counter name (e.g. ``'requests_total'``, ``'errors_total'``).
        value:
            Amount to increment (default 1).
        **attributes:
            Labels attached to the metric (e.g. ``endpoint="/api/foo"``).
        """
        counter = self._counters.get(name)
        if counter is not None:
            try:
                counter.add(value, self._enriched_attributes(attributes))
            except Exception:
                pass

    def record_latency(
        self,
        value_ms: float,
        **attributes: Any,
    ) -> None:
        """
        Record a latency measurement in milliseconds.

        Parameters
        ----------
        value_ms:
            Latency in milliseconds.
        **attributes:
            Labels (e.g. ``operation="parse_cv"``).
        """
        histogram = self._histograms.get("request_latency_ms")
        if histogram is not None:
            try:
                histogram.record(value_ms, self._enriched_attributes(attributes))
            except Exception:
                pass

    def record_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str,
        **attributes: Any,
    ) -> None:
        """
        Record LLM token usage.

        Parameters
        ----------
        input_tokens:
            Number of input/prompt tokens.
        output_tokens:
            Number of output/completion tokens.
        model:
            Model name (e.g. ``'gpt-4'``).
        """
        counter = self._counters.get("llm_tokens_total")
        if counter is not None:
            try:
                base = {"direction": "input", "model": model, **attributes}
                counter.add(input_tokens, self._enriched_attributes(base))
                base["direction"] = "output"
                counter.add(output_tokens, self._enriched_attributes(base))
            except Exception:
                pass

    def record_cost(
        self,
        cost_usd: float,
        model: str,
        **attributes: Any,
    ) -> None:
        """
        Record LLM cost in USD.

        Parameters
        ----------
        cost_usd:
            Total cost in USD.
        model:
            Model name.
        """
        counter = self._counters.get("llm_cost_usd_total")
        if counter is not None:
            try:
                counter.add(
                    cost_usd,
                    self._enriched_attributes({"model": model, **attributes}),
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        if self._provider is None:
            return
        try:
            self._provider.force_flush()
            self._provider.shutdown()
        except Exception as exc:
            print(f"[obs-sdk] metrics shutdown error: {exc}", file=sys.stderr)
