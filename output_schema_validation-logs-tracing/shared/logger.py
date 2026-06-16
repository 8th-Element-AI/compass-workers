import logging
import os
from contextvars import ContextVar

# Context variable — set once at the start of validation, auto-tags
# every log line emitted anywhere in the call stack with schema_name.

_schema_name: ContextVar[str] = ContextVar("schema_name", default="")


def set_schema_context(schema_name: str) -> None:
    _schema_name.set(schema_name)


def clear_schema_context() -> None:
    _schema_name.set("")


# Filter — injects schema_name from ContextVar into every log record
# so LokiHandler can forward it as a structured field.

class SchemaContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.schema_name = _schema_name.get() or ""
        return True

# LokiHandler — bridge between standard Python logging and obs_sdk.
# Intercepts every logger.info/warning/error() call and routes it to
# obs.log.info/warning/error() which forwards to Loki via OTel Collector.

class LokiHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            obs = _get_obs()
            msg = record.getMessage()
            extra = {
                "logger": record.name,
                "lineno": record.lineno,
            }
            schema = getattr(record, "schema_name", "") or ""
            if schema:
                extra["schema_name"] = schema

            level = record.levelname
            if level == "DEBUG":
                obs.log.debug(msg, **extra)
            elif level in ("WARNING", "WARN"):
                obs.log.warning(msg, **extra)
            elif level in ("ERROR", "CRITICAL"):
                obs.log.error(msg, **extra)
            else:
                obs.log.info(msg, **extra)
        except Exception:
            self.handleError(record)


# Singleton ObservabilityClient — created once, reused across all loggers.
# Falls back to a no-op stub if obs_sdk is unavailable or misconfigured.
_obs = None


def _get_obs():
    global _obs
    if _obs is not None:
        return _obs
    try:
        from obs_sdk import ObservabilityClient
        _obs = ObservabilityClient(
            service_name=os.getenv("OBS_SERVICE_NAME", "output-schema-validation"),
            environment=os.getenv("OBS_ENVIRONMENT", "development"),
            loki_endpoint=os.getenv("OBS_LOKI_ENDPOINT", ""),
            tempo_endpoint=os.getenv("OBS_TEMPO_ENDPOINT", ""),
        )
    except Exception as exc:
        print(f"[shared.logger] ObservabilityClient init failed: {exc}")
        _obs = _NoOpObs()
    return _obs


class _NoOpLog:
    def info(self, *a, **kw): pass
    def debug(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def warn(self, *a, **kw): pass
    def error(self, *a, **kw): pass


class _NoOpObs:
    log = _NoOpLog()

# Setup — attaches LokiHandler to the root logger once (idempotent).
def _setup_loki_handler() -> None:
    root = logging.getLogger()
    if any(isinstance(h, LokiHandler) for h in root.handlers):
        return
    handler = LokiHandler()
    handler.addFilter(SchemaContextFilter())
    root.addHandler(handler)


# Public API — use this instead of logging.getLogger() directly.
def get_logger(name: str) -> logging.Logger:
    _setup_loki_handler()
    return logging.getLogger(name)


def get_obs():
    return _get_obs()
