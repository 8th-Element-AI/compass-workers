"""
ObservabilityClient — the single entry point developers interact with.

Uses a plugin architecture: built-in plugins (tracing, logging, metrics,
llm) are loaded automatically.  Custom plugins can be added and built-in
ones can be excluded.

Usage::

    from obs_sdk import ObservabilityClient

    obs = ObservabilityClient(service_name="my-service")

    with obs.trace("my-span", {"key": "value"}):
        obs.log.info("Hello", user="alice")
        result = obs.llm(
            model="gpt-4",
            prompt="Say hi",
            call=lambda: my_openai_call(),
        )
        obs.score("quality", 0.9)
        obs.metrics.increment("requests_total")

    obs.shutdown()
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, List, Optional, Union

from obs_sdk.config import ObsConfig
from obs_sdk import context as _ctx
from obs_sdk.context import get_trace_id, set_trace_id
from obs_sdk.extractors.registry import ExtractorRegistry


# Built-in plugin load order — dependencies must come before dependents.
# "tracing" is first because logging and metrics reuse its OTel Resource.
# "llm" is last because it calls all three other plugins.
_BUILTIN_PLUGIN_ORDER = ["tracing", "logging", "metrics", "llm"]


class _NoOpLog:
    """Stub logger returned when the logging plugin is not loaded.

    Accepts all the same method calls as LoggingPlugin but does nothing.
    This lets calling code use obs.log.info(...) without checking if
    logging is enabled.
    """
    def info(self, message: str, **kwargs: Any) -> None: pass
    def warn(self, message: str, **kwargs: Any) -> None: pass
    def warning(self, message: str, **kwargs: Any) -> None: pass
    def error(self, message: str, **kwargs: Any) -> None: pass
    def debug(self, message: str, **kwargs: Any) -> None: pass


class _NoOpMetrics:
    """Stub metrics returned when the metrics plugin is not loaded.

    Accepts all the same method calls as MetricsPlugin but does nothing.
    This lets calling code use obs.metrics.increment(...) without checking
    if metrics is enabled.
    """
    def increment(self, name: str, value: int = 1, **attrs: Any) -> None: pass
    def record_latency(self, value_ms: float, **attrs: Any) -> None: pass
    def record_tokens(self, inp: int, out: int, model: str, **attrs: Any) -> None: pass
    def record_cost(self, cost: float, model: str, **attrs: Any) -> None: pass


class ObservabilityClient:
    """
    Unified observability client combining tracing, logging, metrics,
    and LLM observability via a plugin architecture.

    Parameters
    ----------
    config:
        An :class:`~obs_sdk.config.ObsConfig` instance, a plain ``dict``
        (must contain ``service_name``), or ``None`` to read everything
        from environment variables.
    plugins:
        Optional list of additional plugin *instances* to load after
        the built-in ones.
    exclude_plugins:
        Optional list of built-in plugin names to skip
        (e.g. ``["metrics"]``).
    **kwargs:
        Any :class:`~obs_sdk.config.ObsConfig` field as a keyword argument.
    """

    def __init__(
        self,
        config: Union[ObsConfig, dict, None] = None,
        plugins: Optional[List[Any]] = None,
        exclude_plugins: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        # Step 1: Build a unified config from constructor args + env vars
        resolved = self._resolve_config(config, kwargs)
        self._config = resolved
        self._plugins: Dict[str, Any] = {}

        # Importing PluginRegistry triggers the @obs_plugin decorators
        # which register TracingPlugin, LoggingPlugin, MetricsPlugin, LLMPlugin
        from obs_sdk.plugins.base import PluginRegistry

        exclude = set(exclude_plugins or [])

        # Step 2: Load built-in plugins in dependency order.
        # Each plugin receives all previously-loaded plugins as **deps,
        # so LLMPlugin gets tracing, logging, and metrics references.
        for name in _BUILTIN_PLUGIN_ORDER:
            if name in exclude:
                continue
            plugin_cls = PluginRegistry.get_class(name)
            if plugin_cls is None:
                continue
            plugin = plugin_cls()
            deps = {n: p for n, p in self._plugins.items()}
            try:
                plugin.initialize(resolved, **deps)
                self._plugins[name] = plugin
            except Exception as exc:
                print(
                    f"[obs-sdk] Plugin '{name}' init failed: {exc}",
                    file=sys.stderr,
                )

        # Step 3: Load any user-provided custom plugins
        for plugin in (plugins or []):
            deps = {n: p for n, p in self._plugins.items()}
            try:
                plugin.initialize(resolved, **deps)
                self._plugins[plugin.name] = plugin
            except Exception as exc:
                print(
                    f"[obs-sdk] Plugin '{plugin.name}' init failed: {exc}",
                    file=sys.stderr,
                )

    # ------------------------------------------------------------------ #
    # Public surface                                                       #
    # ------------------------------------------------------------------ #

    @property
    def log(self) -> Any:
        """The logging plugin — use as ``obs.log.info(...)``."""
        return self._plugins.get("logging") or _NoOpLog()

    @property
    def metrics(self) -> Any:
        """The metrics plugin — use as ``obs.metrics.increment(...)``."""
        return self._plugins.get("metrics") or _NoOpMetrics()

    def trace(
        self,
        name: str,
        attributes: Optional[dict] = None,
        *,
        correlation_id: Optional[str] = None,
        entity_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_version: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> Any:
        """
        Start a trace span.  Works as context manager and decorator.

        Falls back to a no-op context manager if tracing is disabled.
        """
        tracing = self._plugins.get("tracing")
        if tracing is not None:
            return tracing.trace(
                name, attributes,
                correlation_id=correlation_id,
                entity_id=entity_id,
                entity_type=entity_type,
                entity_version=entity_version,
                stage=stage,
            )
        # No-op fallback
        from contextlib import nullcontext
        return nullcontext()

    def llm(
        self,
        model: str,
        prompt: Union[str, list[dict]],
        call: Callable[[], Any],
        name: str = "llm-call",
        metadata: Optional[dict] = None,
    ) -> Any:
        """Execute an LLM call with full observability."""
        llm_plugin = self._plugins.get("llm")
        if llm_plugin is not None:
            return llm_plugin.llm(
                model=model,
                prompt=prompt,
                call=call,
                name=name,
                metadata=metadata,
            )
        # No LLM plugin — just execute the call
        return call()

    def score(
        self,
        name: str,
        value: float,
        trace_id: Optional[str] = None,
        comment: str = "",
    ) -> None:
        """Post a quality score for the current (or specified) trace."""
        llm_plugin = self._plugins.get("llm")
        if llm_plugin is not None:
            llm_plugin.score(
                name=name, value=value, trace_id=trace_id, comment=comment
            )

    def set_attribute(self, key: str, value: Any) -> None:
        """Set an attribute on the currently active OTel span."""
        tracing = self._plugins.get("tracing")
        if tracing is not None:
            tracing.set_attribute(key, value)

    def add_event(
        self,
        name: str,
        attributes: Optional[dict] = None,
    ) -> None:
        """Add a timestamped event to the currently active OTel span."""
        tracing = self._plugins.get("tracing")
        if tracing is not None:
            tracing.add_event(name, attributes)

    def get_trace_id(self) -> Optional[str]:
        """Return the current context traceID, or ``None``."""
        return get_trace_id()

    def set_correlation_id(self, correlation_id: str) -> None:
        """Set the correlation ID for the current context."""
        _ctx.set_correlation_id(correlation_id)

    def get_correlation_id(self) -> Optional[str]:
        """Return the current correlation ID, or ``None``."""
        return _ctx.get_correlation_id()

    def set_entity(
        self,
        entity_id: str,
        entity_type: Optional[str] = None,
        entity_version: Optional[str] = None,
    ) -> None:
        """Set the entity context for the current context."""
        _ctx.set_entity(entity_id, entity_type, entity_version)

    def get_entity(self) -> Dict[str, Any]:
        """Return the current entity context as a dict."""
        return _ctx.get_entity()

    def get_plugin(self, name: str) -> Optional[Any]:
        """Return a loaded plugin by name, or ``None``."""
        return self._plugins.get(name)

    def register_token_extractor(self, extractor: Callable) -> None:
        """
        Register a custom token/text extractor for LLM responses.

        Custom extractors take priority over built-in ones.
        """
        ExtractorRegistry.register(extractor)

    def shutdown(self) -> None:
        """Gracefully shut down all plugins."""
        # Shutdown in reverse order (dependents first)
        for name in reversed(list(self._plugins.keys())):
            try:
                self._plugins[name].shutdown()
            except Exception as exc:
                print(
                    f"[obs-sdk] {name} shutdown error: {exc}",
                    file=sys.stderr,
                )

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_config(
        config: Union[ObsConfig, dict, None],
        kwargs: dict,
    ) -> ObsConfig:
        """
        Merge config sources with priority: kwargs > config arg > env vars.
        """
        if isinstance(config, ObsConfig):
            base = config
        elif isinstance(config, dict):
            merged = dict(config)
            merged.update(kwargs)
            return ObsConfig.from_dict(merged) if merged else ObservabilityClient._from_env_plus_kwargs(kwargs)
        else:
            return ObservabilityClient._from_env_plus_kwargs(kwargs)

        if kwargs:
            import dataclasses
            current = dataclasses.asdict(base)
            current.update(kwargs)
            return ObsConfig.from_dict(current)

        return base

    @staticmethod
    def _from_env_plus_kwargs(kwargs: dict) -> ObsConfig:
        """Build ObsConfig from environment variables, then apply kwargs."""
        service_name = (
            kwargs.get("service_name")
            or os.environ.get("OBS_SERVICE_NAME", "")
        )
        if not service_name:
            raise ValueError(
                "service_name must be provided either as a kwarg or via "
                "the OBS_SERVICE_NAME environment variable."
            )
        base = ObsConfig(service_name=service_name)
        import dataclasses
        base_dict = dataclasses.asdict(base)
        base_dict.update(kwargs)
        return ObsConfig.from_dict(base_dict)
