"""
LoggingPlugin — structured logging via stdout + OTel Logs SDK.

Logs are emitted two ways:
1. Structured JSON to stdout via Python's logging.Logger (Promtail picks
   these up and ships to Loki as a secondary/redundant path).
2. OTel LogRecords via the OTel Logs SDK → OTel Collector → Loki
   (primary path — no background threads or direct HTTP needed).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from obs_sdk.config import ObsConfig
from obs_sdk import context as ctx
from obs_sdk.plugins.base import ObsPlugin, obs_plugin

try:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggingHandler
    _OTEL_LOGS_AVAILABLE = True
except ImportError:
    _OTEL_LOGS_AVAILABLE = False


@obs_plugin("logging")
class LoggingPlugin(ObsPlugin):
    """Structured logging with stdout + OTel Logs SDK export."""

    @property
    def name(self) -> str:
        return "logging"

    def initialize(self, config: ObsConfig, **dependencies: Any) -> None:
        """Set up the logging pipeline with two output paths.

        Path 1 (always): StreamHandler → prints structured JSON to stdout
        Path 2 (if OTel available): OTel LoggingHandler → BatchLogRecordProcessor
            → OTLPLogExporter → OTel Collector → Loki

        Both paths fire from a single self._std_logger.log() call, because
        the Python logger has two handlers attached.  The OTel handler reuses
        the same Resource from TracingPlugin (passed via dependencies) so that
        service_name is consistent across traces and logs.
        """
        self._config = config
        self._log_provider: Any = None

        # Standard Python logger for structured JSON to stdout
        self._std_logger = logging.getLogger(f"obs_sdk.{config.service_name}")
        if not self._std_logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            self._std_logger.addHandler(handler)
        self._std_logger.setLevel(
            getattr(logging, config.log_level.upper(), logging.INFO)
        )
        self._std_logger.propagate = False

        # OTel Logs SDK — sends log records through the Collector to Loki
        if _OTEL_LOGS_AVAILABLE and config.otel_logs_enabled:
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

                log_exporter = OTLPLogExporter(
                    endpoint=f"{config.otel_collector_endpoint}/v1/logs"
                )
                self._log_provider = LoggerProvider(resource=resource)
                self._log_provider.add_log_record_processor(
                    BatchLogRecordProcessor(log_exporter)
                )

                otel_handler = LoggingHandler(
                    level=logging.NOTSET,
                    logger_provider=self._log_provider,
                )
                self._std_logger.addHandler(otel_handler)
            except Exception as exc:
                print(
                    f"[obs-sdk] OTel Logs init failed — stdout-only logging: {exc}",
                    file=sys.stderr,
                )

    # ------------------------------------------------------------------ #
    # Public log methods                                                   #
    # ------------------------------------------------------------------ #

    def info(self, message: str, **kwargs: Any) -> None:
        """Emit an INFO-level log line."""
        self._emit("INFO", message, **kwargs)

    def warn(self, message: str, **kwargs: Any) -> None:
        """Emit a WARN-level log line."""
        self._emit("WARN", message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """Alias for :meth:`warn`."""
        self._emit("WARN", message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """Emit an ERROR-level log line."""
        self._emit("ERROR", message, **kwargs)

    def debug(self, message: str, **kwargs: Any) -> None:
        """Emit a DEBUG-level log line."""
        self._emit("DEBUG", message, **kwargs)

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _emit(self, level: str, message: str, **kwargs: Any) -> None:
        """Build a structured JSON log payload and emit it.

        Automatically attaches the current trace_id from contextvars — this is
        how logs get correlated with traces in Grafana without the caller
        having to pass trace_id manually.

        The kwargs (e.g. llm_model, llm_cost_usd) are merged into the JSON,
        so Loki can parse and filter on them.
        """
        trace_id = ctx.get_trace_id() or ""
        correlation_id = ctx.get_correlation_id() or ""
        now = datetime.now(timezone.utc)
        timestamp_iso = now.isoformat()

        log_dict: dict[str, Any] = {
            "message": message,
            "level": level,
            "timestamp": timestamp_iso,
            "service": self._config.service_name,
            "environment": self._config.environment,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            **kwargs,
        }

        # Add entity context fields only when set (avoid noise)
        entity = ctx.get_entity()
        if entity.get("entity_id") is not None:
            log_dict["entity_id"] = entity["entity_id"]
        if entity.get("entity_type") is not None:
            log_dict["entity_type"] = entity["entity_type"]
        if entity.get("entity_version") is not None:
            log_dict["entity_version"] = entity["entity_version"]

        log_line_str = json.dumps(log_dict, separators=(",", ":"))
        std_level = getattr(
            logging, level if level != "WARN" else "WARNING", logging.INFO
        )

        # This single call emits to BOTH:
        # - StreamHandler → stdout (Promtail picks up)
        # - OTel LoggingHandler → Collector → Loki (if configured)
        self._std_logger.log(std_level, log_line_str)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        """Flush pending OTel log records and shut down."""
        if self._log_provider is not None:
            try:
                self._log_provider.force_flush()
                self._log_provider.shutdown()
            except Exception as exc:
                print(f"[obs-sdk] logger shutdown error: {exc}", file=sys.stderr)
