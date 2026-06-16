"""
obs-sdk — Unified observability SDK for Tempo, Loki, Langfuse, and Prometheus.

Quick start::

    from obs_sdk import ObservabilityClient

    obs = ObservabilityClient(service_name="my-service")

    with obs.trace("my-operation"):
        obs.log.info("Processing request", user_id="u123")
        result = obs.llm(model="gpt-4", prompt="Hello", call=lambda: ...)
        obs.score("quality", 0.9)
        obs.metrics.increment("requests_total")

    obs.shutdown()
"""

from obs_sdk.config import ObsConfig
from obs_sdk.client import ObservabilityClient
from obs_sdk.plugins.base import ObsPlugin, PluginRegistry
from obs_sdk.extractors.registry import ExtractorRegistry

__all__ = [
    "ObservabilityClient",
    "ObsConfig",
    "ObsPlugin",
    "PluginRegistry",
    "ExtractorRegistry",
]
__version__ = "0.2.0"
