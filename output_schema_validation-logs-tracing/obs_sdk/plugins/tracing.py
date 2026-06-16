"""
TracingPlugin — sends spans to Tempo via OTel Collector.

Wraps the OpenTelemetry Python SDK.  The OTLP exporter points at the
OTel Collector (``otel_collector_endpoint``).  Falls back to
``tempo_endpoint`` for backwards compatibility.
"""

from __future__ import annotations

import sys
import functools
import inspect
from contextlib import contextmanager
from typing import Any, Callable, Optional

from obs_sdk.config import ObsConfig
from obs_sdk import context as ctx
from obs_sdk.plugins.base import ObsPlugin, obs_plugin

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ALWAYS_ON
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.trace import Status, StatusCode, NonRecordingSpan
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


class _SpanContextManager:
    """Internal context manager returned by ``start_span``.

    Manages the lifecycle of a single OTel span:
    - On __enter__: span is already started (done in start_span)
    - On __exit__:  marks span OK or ERROR, ends it, restores previous trace_id

    Also handles trace_id propagation — saves the previous trace_id on enter
    and restores it on exit, so nested spans don't leak their IDs upward.
    """

    def __init__(
        self,
        span: Any,
        previous_trace_id: Optional[str],
        otel_ctx_token: Any,
        previous_correlation_id: Optional[str] = None,
        restore_correlation: bool = False,
        previous_entity: Optional[dict] = None,
        restore_entity: bool = False,
    ) -> None:
        self._span = span
        self._previous_trace_id = previous_trace_id
        self._otel_ctx_token = otel_ctx_token
        self._previous_correlation_id = previous_correlation_id
        self._restore_correlation = restore_correlation
        self._previous_entity = previous_entity
        self._restore_entity = restore_entity

    def __enter__(self) -> "_SpanContextManager":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if _OTEL_AVAILABLE and self._span is not None:
            try:
                if exc_type is not None:
                    self._span.set_status(
                        Status(StatusCode.ERROR, str(exc_val) if exc_val else "")
                    )
                    self._span.record_exception(exc_val)
                else:
                    self._span.set_status(Status(StatusCode.OK))
            except Exception:
                pass
            finally:
                try:
                    self._span.end()
                except Exception:
                    pass
                if self._otel_ctx_token is not None:
                    try:
                        from opentelemetry import context as otel_context
                        otel_context.detach(self._otel_ctx_token)
                    except Exception:
                        pass
        # Restore previous traceID
        if self._previous_trace_id is not None:
            ctx.set_trace_id(self._previous_trace_id)
        else:
            ctx.clear_trace_id()
        # Restore previous correlation ID if this span set a new one
        if self._restore_correlation:
            if self._previous_correlation_id is not None:
                ctx.set_correlation_id(self._previous_correlation_id)
            else:
                ctx.clear_correlation_id()
        # Restore previous entity context if this span set new ones
        if self._restore_entity:
            if self._previous_entity is not None:
                prev = self._previous_entity
                if prev.get("entity_id") is not None:
                    ctx.set_entity(
                        prev["entity_id"],
                        prev.get("entity_type"),
                        prev.get("entity_version"),
                    )
                else:
                    ctx.clear_entity()
            else:
                ctx.clear_entity()
        return False


class _TraceProxy:
    """Dual-mode object: works as both a context manager and a decorator.

    As context manager:
        with obs.trace("my-span"):
            ...

    As decorator:
        @obs.trace("my-span")
        def my_function():
            ...

    In both cases, a span is created on enter and ended on exit.
    """

    def __init__(
        self,
        plugin: "TracingPlugin",
        name: str,
        attributes: Optional[dict],
        *,
        correlation_id: Optional[str] = None,
        entity_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_version: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> None:
        self._plugin = plugin
        self._name = name
        self._attributes = attributes
        self._ctx_kwargs = dict(
            correlation_id=correlation_id,
            entity_id=entity_id,
            entity_type=entity_type,
            entity_version=entity_version,
            stage=stage,
        )
        self._cm: Optional[_SpanContextManager] = None

    def __enter__(self) -> "_TraceProxy":
        self._cm = self._plugin.start_span(self._name, self._attributes, **self._ctx_kwargs)
        self._cm.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if self._cm is not None:
            return self._cm.__exit__(exc_type, exc_val, exc_tb)
        return False

    async def __aenter__(self) -> "_TraceProxy":
        self._cm = self._plugin.start_span(self._name, self._attributes, **self._ctx_kwargs)
        self._cm.__enter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if self._cm is not None:
            return self._cm.__exit__(exc_type, exc_val, exc_tb)
        return False

    def __call__(self, func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with self._plugin.start_span(self._name, self._attributes, **self._ctx_kwargs):
                    return await func(*args, **kwargs)
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with self._plugin.start_span(self._name, self._attributes, **self._ctx_kwargs):
                    return func(*args, **kwargs)
            return sync_wrapper


@obs_plugin("tracing")
class TracingPlugin(ObsPlugin):
    """Distributed tracing via OpenTelemetry → OTel Collector → Tempo."""

    @property
    def name(self) -> str:
        return "tracing"

    def initialize(self, config: ObsConfig, **dependencies: Any) -> None:
        self._config = config
        self._tracer: Any = None
        self._provider: Any = None
        self._resource: Any = None

        if not _OTEL_AVAILABLE:
            print(
                "[obs-sdk] opentelemetry packages not installed — tracing disabled.",
                file=sys.stderr,
            )
            return

        self._setup()

    def _setup(self) -> None:
        """Set up the OTel tracing pipeline.

        Creates: Resource (service identity) → TracerProvider → BatchSpanProcessor
        → OTLPSpanExporter (sends spans via HTTP to the OTel Collector).

        The Resource tags every span with service.name and deployment.environment
        so backends (Tempo, Grafana) can identify which service produced the span.
        """
        try:
            self._resource = Resource.create({
                "service.name": self._config.service_name,
                "deployment.environment": self._config.environment,
            })

            rate = self._config.trace_sample_rate
            sampler = ALWAYS_ON if rate >= 1.0 else TraceIdRatioBased(rate)

            # Prefer OTel Collector; fall back to direct Tempo endpoint
            endpoint = self._config.tempo_endpoint or (
                f"{self._config.otel_collector_endpoint}/v1/traces"
            )

            exporter = OTLPSpanExporter(endpoint=endpoint, timeout=3)

            self._provider = TracerProvider(resource=self._resource, sampler=sampler)
            self._provider.add_span_processor(
                BatchSpanProcessor(exporter, export_timeout_millis=3000)
            )

            trace.set_tracer_provider(self._provider)
            self._tracer = self._provider.get_tracer(
                instrumenting_module_name=self._config.service_name
            )
        except Exception as exc:
            print(f"[obs-sdk] Failed to initialise tracer: {exc}", file=sys.stderr)
            self._tracer = None
            self._provider = None

    @property
    def resource(self) -> Any:
        """The OTel Resource — shared by other plugins that need it."""
        return self._resource

    def start_span(
        self,
        name: str,
        attributes: Optional[dict] = None,
        *,
        correlation_id: Optional[str] = None,
        entity_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_version: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> _SpanContextManager:
        """Start a new OTel span and return a context manager."""
        previous_trace_id = ctx.get_trace_id()

        # Save and set correlation ID if provided
        previous_correlation_id = ctx.get_correlation_id()
        restore_correlation = False
        if correlation_id is not None:
            ctx.set_correlation_id(correlation_id)
            restore_correlation = True

        # Save and set entity context if provided
        previous_entity = ctx.get_entity()
        restore_entity = False
        if entity_id is not None:
            ctx.set_entity(entity_id, entity_type, entity_version)
            restore_entity = True

        # Build context attributes to attach to the span
        context_attrs: dict[str, Any] = {}
        cur_corr = ctx.get_correlation_id()
        if cur_corr is not None:
            context_attrs["correlation_id"] = cur_corr
        cur_entity = ctx.get_entity()
        if cur_entity.get("entity_id") is not None:
            context_attrs["entity.id"] = cur_entity["entity_id"]
        if cur_entity.get("entity_type") is not None:
            context_attrs["entity.type"] = cur_entity["entity_type"]
        if cur_entity.get("entity_version") is not None:
            context_attrs["entity.version"] = cur_entity["entity_version"]
        if stage is not None:
            context_attrs["pipeline.stage"] = stage

        merged_attrs = dict(attributes or {})
        merged_attrs.update(context_attrs)

        restore_kwargs = dict(
            previous_correlation_id=previous_correlation_id,
            restore_correlation=restore_correlation,
            previous_entity=previous_entity,
            restore_entity=restore_entity,
        )

        if self._tracer is None:
            generated = ctx.generate_trace_id()
            ctx.set_trace_id(generated)
            return _SpanContextManager(
                span=None,
                previous_trace_id=previous_trace_id,
                otel_ctx_token=None,
                **restore_kwargs,
            )

        try:
            from opentelemetry import context as otel_context

            span = self._tracer.start_span(name, attributes=merged_attrs)
            otel_ctx = trace.set_span_in_context(span)
            token = otel_context.attach(otel_ctx)

            span_ctx = span.get_span_context()
            if span_ctx and span_ctx.trace_id:
                trace_id_hex = format(span_ctx.trace_id, "032x")
                ctx.set_trace_id(trace_id_hex)
            else:
                ctx.set_trace_id(ctx.generate_trace_id())

        except Exception as exc:
            print(f"[obs-sdk] start_span error: {exc}", file=sys.stderr)
            ctx.set_trace_id(ctx.generate_trace_id())
            return _SpanContextManager(
                span=None,
                previous_trace_id=previous_trace_id,
                otel_ctx_token=None,
                **restore_kwargs,
            )

        return _SpanContextManager(
            span=span,
            previous_trace_id=previous_trace_id,
            otel_ctx_token=token,
            **restore_kwargs,
        )

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
    ) -> _TraceProxy:
        """Start a trace span — works as both context manager and decorator."""
        return _TraceProxy(
            self, name, attributes,
            correlation_id=correlation_id,
            entity_id=entity_id,
            entity_type=entity_type,
            entity_version=entity_version,
            stage=stage,
        )

    def set_attribute(self, key: str, value: Any) -> None:
        if not _OTEL_AVAILABLE:
            return
        try:
            span = trace.get_current_span()
            if span and not isinstance(span, NonRecordingSpan):
                span.set_attribute(key, value)
        except Exception as exc:
            print(f"[obs-sdk] set_attribute error: {exc}", file=sys.stderr)

    def add_event(self, name: str, attributes: Optional[dict] = None) -> None:
        if not _OTEL_AVAILABLE:
            return
        try:
            span = trace.get_current_span()
            if span and not isinstance(span, NonRecordingSpan):
                span.add_event(name, attributes=attributes or {})
        except Exception as exc:
            print(f"[obs-sdk] add_event error: {exc}", file=sys.stderr)

    def shutdown(self) -> None:
        if self._provider is None:
            return
        try:
            self._provider.force_flush()
            self._provider.shutdown()
        except Exception as exc:
            print(f"[obs-sdk] tracer shutdown error: {exc}", file=sys.stderr)
