"""
Context propagation for the obs-sdk.

A single traceID flows through every trace, log line, and LLM call in
the same request without any manual ID passing by the developer.

Uses Python's ``contextvars.ContextVar`` — works correctly in both
synchronous and asyncio-based applications.  threading.local is never
used here.

W3C TraceContext propagation helpers are also provided for cross-service
trace correlation (inject outgoing headers, extract incoming headers).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional

# The single ContextVar that carries the active traceID.
# This is the mechanism that lets obs.log.info(), obs.llm(), etc. automatically
# know which trace they belong to — without the caller passing trace_id manually.
# Works across threads and async tasks because each gets its own copy.
_trace_id_var: ContextVar[str | None] = ContextVar("obs_trace_id", default=None)

# Correlation ID — ties all work in a single process run together.
# Enables queries like "show me everything from this batch run".
_correlation_id_var: ContextVar[str | None] = ContextVar("obs_correlation_id", default=None)

# Entity context — identifies the business entity being processed.
# Enables queries like "show me everything for document X".
_entity_id_var: ContextVar[str | None] = ContextVar("obs_entity_id", default=None)
_entity_type_var: ContextVar[str | None] = ContextVar("obs_entity_type", default=None)
_entity_version_var: ContextVar[str | None] = ContextVar("obs_entity_version", default=None)


def set_trace_id(trace_id: str) -> None:
    """
    Store *trace_id* as the active traceID for the current context.

    Overwrites any previously stored value without returning a token;
    the tracer is responsible for restoring context on span exit.
    """
    _trace_id_var.set(trace_id)


def get_trace_id() -> str | None:
    """
    Return the active traceID for the current context, or ``None``.

    Safe to call from any thread or asyncio task — each has its own
    context slot.
    """
    return _trace_id_var.get()


def clear_trace_id() -> None:
    """Reset the active traceID to ``None`` for the current context."""
    _trace_id_var.set(None)


def generate_trace_id() -> str:
    """
    Generate a new random traceID as a 32-character hex string (no dashes).

    Example: ``'4b3f9e1a2c7d8e5f6a0b1c2d3e4f5a6b'``
    """
    return uuid.uuid4().hex


# -------------------------------------------------------------------- #
# Correlation ID helpers                                                 #
# -------------------------------------------------------------------- #

def set_correlation_id(correlation_id: str) -> None:
    """Store *correlation_id* as the active correlation ID for the current context."""
    _correlation_id_var.set(correlation_id)


def get_correlation_id() -> str | None:
    """Return the active correlation ID for the current context, or ``None``."""
    return _correlation_id_var.get()


def clear_correlation_id() -> None:
    """Reset the active correlation ID to ``None`` for the current context."""
    _correlation_id_var.set(None)


def generate_correlation_id() -> str:
    """Generate a new random correlation ID as a 32-character hex string."""
    return uuid.uuid4().hex


# -------------------------------------------------------------------- #
# Entity context helpers                                                 #
# -------------------------------------------------------------------- #

def set_entity(
    entity_id: str,
    entity_type: Optional[str] = None,
    entity_version: Optional[str] = None,
) -> None:
    """Store entity context (id, type, version) for the current context."""
    _entity_id_var.set(entity_id)
    if entity_type is not None:
        _entity_type_var.set(entity_type)
    if entity_version is not None:
        _entity_version_var.set(entity_version)


def get_entity() -> Dict[str, str | None]:
    """Return the current entity context as a dict."""
    return {
        "entity_id": _entity_id_var.get(),
        "entity_type": _entity_type_var.get(),
        "entity_version": _entity_version_var.get(),
    }


def clear_entity() -> None:
    """Reset all entity context vars to ``None``."""
    _entity_id_var.set(None)
    _entity_type_var.set(None)
    _entity_version_var.set(None)


# -------------------------------------------------------------------- #
# W3C TraceContext propagation helpers                                    #
# -------------------------------------------------------------------- #

def inject_context(carrier: Dict[str, str]) -> Dict[str, str]:
    """
    Inject W3C TraceContext headers (``traceparent``, ``tracestate``)
    into an outgoing carrier (e.g. HTTP headers dict).

    Uses OTel's global propagator, which is set up automatically when
    the TracerProvider is initialised by the tracing plugin.

    Parameters
    ----------
    carrier:
        A mutable dict of headers.  Modified in-place and returned.

    Returns
    -------
    dict
        The same carrier dict with trace headers injected.
    """
    try:
        from opentelemetry.propagate import inject
        inject(carrier)
    except ImportError:
        pass
    return carrier


def extract_context(carrier: Dict[str, str]) -> Any:
    """
    Extract W3C TraceContext from incoming headers and return an OTel
    ``Context`` object.

    Parameters
    ----------
    carrier:
        A dict of incoming HTTP headers.

    Returns
    -------
    opentelemetry.context.Context
        The extracted context.  Attach it with
        ``opentelemetry.context.attach(ctx)`` to make it active.
    """
    try:
        from opentelemetry.propagate import extract
        return extract(carrier)
    except ImportError:
        return None


def propagating_headers(
    existing_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Convenience helper for outgoing HTTP calls.

    Returns a headers dict with W3C ``traceparent`` / ``tracestate``
    injected alongside any existing headers.

    Usage::

        import requests
        from obs_sdk.context import propagating_headers

        resp = requests.post(url, headers=propagating_headers({"Content-Type": "application/json"}))
    """
    headers = dict(existing_headers or {})
    return inject_context(headers)
