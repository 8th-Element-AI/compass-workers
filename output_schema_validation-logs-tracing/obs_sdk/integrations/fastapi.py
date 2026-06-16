"""
FastAPI middleware for W3C TraceContext propagation.

Extracts ``traceparent`` / ``tracestate`` from incoming request headers
and attaches the OTel context for the duration of the request so that
spans created inside the request handler are children of the caller's
trace.

Usage::

    from fastapi import FastAPI
    from obs_sdk.integrations.fastapi import TraceContextMiddleware

    app = FastAPI()
    app.add_middleware(TraceContextMiddleware)
"""

from __future__ import annotations

from typing import Any

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response
    _STARLETTE_AVAILABLE = True
except ImportError:
    _STARLETTE_AVAILABLE = False

try:
    from opentelemetry import context as otel_context
    from opentelemetry.propagate import extract
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


if _STARLETTE_AVAILABLE:
    class TraceContextMiddleware(BaseHTTPMiddleware):
        """
        Starlette/FastAPI middleware that extracts W3C TraceContext
        from incoming request headers.
        """

        async def dispatch(self, request: Request, call_next: Any) -> Response:
            if not _OTEL_AVAILABLE:
                return await call_next(request)

            # Extract context from incoming headers
            ctx = extract(dict(request.headers))
            token = otel_context.attach(ctx)
            try:
                response = await call_next(request)
                return response
            finally:
                otel_context.detach(token)
