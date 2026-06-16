"""
ObsConfig — configuration dataclass with environment variable fallback.

Explicit constructor arguments always override environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = True) -> bool:
    val = os.environ.get(key, "")
    if not val:
        return default
    return val.lower() in ("1", "true", "yes")


@dataclass
class ObsConfig:
    """
    Configuration for the ObservabilityClient.

    All fields can be set via constructor kwargs or the corresponding
    environment variables listed below.  Explicit kwargs always win.

    Environment variables:
        OBS_SERVICE_NAME          → service_name
        OBS_ENVIRONMENT           → environment
        OBS_OTEL_ENDPOINT         → otel_collector_endpoint
        OBS_TEMPO_ENDPOINT        → tempo_endpoint  (backwards-compat override)
        OBS_LOKI_ENDPOINT         → loki_endpoint   (legacy, kept for reference)
        OBS_LANGFUSE_PUBLIC_KEY   → langfuse_public_key
        OBS_LANGFUSE_SECRET_KEY   → langfuse_secret_key
        OBS_LANGFUSE_HOST         → langfuse_host
        OBS_METRICS_ENABLED       → metrics_enabled
        OBS_OTEL_LOGS_ENABLED     → otel_logs_enabled
    """

    # ------------------------------------------------------------------ #
    # Required                                                             #
    # ------------------------------------------------------------------ #
    service_name: str

    # ------------------------------------------------------------------ #
    # OTel Collector (primary telemetry endpoint)                          #
    # ------------------------------------------------------------------ #
    otel_collector_endpoint: str = field(
        default_factory=lambda: _env("OBS_OTEL_ENDPOINT", "http://localhost:4318")
    )

    # ------------------------------------------------------------------ #
    # Backwards-compat: if set, overrides Collector for traces             #
    # ------------------------------------------------------------------ #
    tempo_endpoint: str = field(
        default_factory=lambda: _env("OBS_TEMPO_ENDPOINT", "")
    )

    # ------------------------------------------------------------------ #
    # Legacy Loki config (kept for reference; logs now go via Collector)   #
    # ------------------------------------------------------------------ #
    loki_endpoint: str = field(
        default_factory=lambda: _env("OBS_LOKI_ENDPOINT", "http://localhost:3100")
    )
    loki_push_path: str = "/loki/api/v1/push"

    # ------------------------------------------------------------------ #
    # Langfuse (LLM observability — pinned to v2 server + SDK)            #
    # ------------------------------------------------------------------ #
    langfuse_public_key: str = field(
        default_factory=lambda: _env("OBS_LANGFUSE_PUBLIC_KEY", "")
    )
    langfuse_secret_key: str = field(
        default_factory=lambda: _env("OBS_LANGFUSE_SECRET_KEY", "")
    )
    langfuse_host: str = field(
        default_factory=lambda: _env("OBS_LANGFUSE_HOST", "https://cloud.langfuse.com")
    )

    # ------------------------------------------------------------------ #
    # Feature flags                                                        #
    # ------------------------------------------------------------------ #
    environment: str = field(
        default_factory=lambda: _env("OBS_ENVIRONMENT", "development")
    )
    log_level: str = "INFO"
    trace_sample_rate: float = 1.0
    metrics_enabled: bool = field(
        default_factory=lambda: _env_bool("OBS_METRICS_ENABLED", True)
    )
    otel_logs_enabled: bool = field(
        default_factory=lambda: _env_bool("OBS_OTEL_LOGS_ENABLED", True)
    )

    # ------------------------------------------------------------------ #
    # Factory helpers                                                       #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_env(cls) -> "ObsConfig":
        """
        Build an ObsConfig entirely from environment variables.

        OBS_SERVICE_NAME must be set.
        """
        service_name = os.environ.get("OBS_SERVICE_NAME")
        if not service_name:
            raise ValueError("OBS_SERVICE_NAME environment variable is required")
        return cls(service_name=service_name)

    @classmethod
    def from_dict(cls, d: dict) -> "ObsConfig":
        """Build an ObsConfig from a plain dictionary."""
        if "service_name" not in d:
            raise ValueError("'service_name' key is required in config dict")
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)
