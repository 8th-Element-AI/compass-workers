"""
LLMPlugin — wraps LLM calls with full observability.

Records each call to:
- Tempo (child OTel span via TracingPlugin)
- Loki (structured log line via LoggingPlugin)
- Langfuse (trace + generation via Langfuse v2 SDK)
- Prometheus (token/cost/latency metrics via MetricsPlugin)

Uses the ExtractorRegistry for pluggable token/text extraction.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable, Optional, Union

from obs_sdk.config import ObsConfig
from obs_sdk import context as ctx
from obs_sdk.plugins.base import ObsPlugin, obs_plugin
from obs_sdk.extractors.registry import ExtractorRegistry

try:
    from langfuse import Langfuse
    import langfuse as _langfuse_pkg
    _LANGFUSE_AVAILABLE = True
    _LANGFUSE_V4 = not hasattr(Langfuse, "trace")  # v4+ removed .trace()
except ImportError:
    _LANGFUSE_AVAILABLE = False
    _LANGFUSE_V4 = False


# -------------------------------------------------------------------- #
# Model pricing (USD per token).  Add new models as needed.              #
# Used by _estimate_cost() to calculate input_cost and output_cost       #
# from token counts.  Format: "model-name": (input_$/token, output_$/token)
# -------------------------------------------------------------------- #
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Gemini — (input $/token, output $/token)
    "gemini-2.5-flash":              (0.15e-6,  0.60e-6),
    "gemini-2.5-flash-preview-04-17":(0.15e-6,  0.60e-6),
    "gemini-2.5-pro":                (1.25e-6, 10.00e-6),
    "gemini-2.5-pro-preview-05-06":  (1.25e-6, 10.00e-6),
    "gemini-2.0-flash":              (0.10e-6,  0.40e-6),
    "gemini-2.0-flash-001":          (0.10e-6,  0.40e-6),
    "gemini-1.5-flash":              (0.075e-6, 0.30e-6),
    "gemini-1.5-flash-001":          (0.075e-6, 0.30e-6),
    "gemini-1.5-flash-002":          (0.075e-6, 0.30e-6),
    "gemini-1.5-pro":                (1.25e-6,  5.00e-6),
    "gemini-1.5-pro-001":            (1.25e-6,  5.00e-6),
    "gemini-1.5-pro-002":            (1.25e-6,  5.00e-6),
    # OpenAI
    "gpt-4o":                        (2.50e-6, 10.00e-6),
    "gpt-4o-2024-11-20":             (2.50e-6, 10.00e-6),
    "gpt-4o-mini":                   (0.15e-6,  0.60e-6),
    "gpt-4o-mini-2024-07-18":        (0.15e-6,  0.60e-6),
    "gpt-4-turbo":                   (10.0e-6, 30.00e-6),
    "gpt-4":                         (30.0e-6, 60.00e-6),
    "gpt-3.5-turbo":                 (0.50e-6,  1.50e-6),
    # Anthropic
    "claude-sonnet-4-20250514":      (3.00e-6, 15.00e-6),
    "claude-3-5-sonnet-20241022":    (3.00e-6, 15.00e-6),
    "claude-3-haiku-20240307":       (0.25e-6,  1.25e-6),
}


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> tuple[float, float, float]:
    """Calculate the USD cost of an LLM call from token counts.

    Looks up the model in _MODEL_PRICING.  If not found, tries prefix
    matching (e.g. "gemini-2.5-flash-preview-04-17" matches "gemini-2.5-flash").

    Returns (input_cost, output_cost, total_cost) in USD, or (0, 0, 0)
    if the model isn't in the pricing table.
    """
    pricing = _MODEL_PRICING.get(model)
    if pricing is None:
        for key, val in _MODEL_PRICING.items():
            if model.startswith(key):
                pricing = val
                break
    if pricing is None:
        return 0.0, 0.0, 0.0
    in_cost = input_tokens * pricing[0]
    out_cost = output_tokens * pricing[1]
    return round(in_cost, 8), round(out_cost, 8), round(in_cost + out_cost, 8)


@obs_plugin("llm")
class LLMPlugin(ObsPlugin):
    """LLM observability: Langfuse + Tempo spans + Loki logs + Prometheus metrics."""

    @property
    def name(self) -> str:
        return "llm"

    def initialize(self, config: ObsConfig, **dependencies: Any) -> None:
        """Set up the LLM observability plugin.

        Stores references to the 3 other plugins (tracing, logging, metrics)
        so it can call them during obs.llm().  Also creates the Langfuse client
        for sending LLM generations to Langfuse Cloud.

        Unlike the other plugins, LLMPlugin doesn't create its own OTel pipeline —
        it piggybacks on the existing ones via the plugin references.
        """
        self._config = config
        self._tracing = dependencies.get("tracing")
        self._logging = dependencies.get("logging")
        self._metrics = dependencies.get("metrics")
        self._langfuse: Optional[Any] = None

        if (
            _LANGFUSE_AVAILABLE
            and config.langfuse_public_key
            and config.langfuse_secret_key
        ):
            try:
                self._langfuse = Langfuse(
                    public_key=config.langfuse_public_key,
                    secret_key=config.langfuse_secret_key,
                    host=config.langfuse_host,
                )
            except Exception as exc:
                print(
                    f"[obs-sdk] Langfuse init failed — LLM observability disabled: {exc}",
                    file=sys.stderr,
                )

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def llm(
        self,
        model: str,
        prompt: Union[str, list[dict]],
        call: Callable[[], Any],
        name: str = "llm-call",
        metadata: Optional[dict] = None,
    ) -> Any:
        """
        Execute an LLM call with full observability.

        Returns the original response unchanged.
        """
        # Get the trace_id from contextvars (set by obs.trace() earlier).
        # This is how the Langfuse trace and the OTel span share the same ID.
        trace_id = ctx.get_trace_id() or ctx.generate_trace_id()
        response: Any = None
        error_occurred: Optional[Exception] = None
        start_ms = time.time() * 1000

        # Step 1: Create a child span "llm:<model>" in Tempo via TracingPlugin
        if self._tracing is not None:
            span_cm = self._tracing.start_span(f"llm:{model}", {"llm.model": model})
        else:
            from contextlib import nullcontext
            span_cm = nullcontext()

        with span_cm:
            try:
                response = call()
            except Exception as exc:
                error_occurred = exc
                end_ms = time.time() * 1000
                latency_ms = end_ms - start_ms
                # Record the failure
                if self._metrics is not None:
                    try:
                        self._metrics.increment("errors_total", model=model, operation=name)
                        self._metrics.record_latency(round(latency_ms, 2), model=model, operation=name)
                    except Exception:
                        pass
                if self._logging is not None:
                    try:
                        self._logging._emit(
                            "ERROR",
                            f"LLM call failed: {name} — {type(exc).__name__}: {exc}",
                            llm="true",
                            llm_model=model,
                            llm_latency_ms=round(latency_ms, 2),
                            llm_name=name,
                            llm_error=str(exc),
                        )
                    except Exception:
                        pass
                raise

        end_ms = time.time() * 1000
        latency_ms = end_ms - start_ms

        # Step 2: Extract token counts from the response object.
        # The ExtractorRegistry tries Gemini, OpenAI, and Anthropic extractors
        # in sequence until one recognizes the response shape.
        input_tokens, output_tokens, output_text = ExtractorRegistry.extract(
            response, prompt
        )

        # Step 3: Calculate cost from the built-in pricing table
        input_cost, output_cost, total_cost = _estimate_cost(
            model, input_tokens, output_tokens
        )

        # Step 4: Attach metadata to the OTel span → shows up in Tempo
        if self._tracing is not None:
            try:
                self._tracing.set_attribute("llm.model", model)
                self._tracing.set_attribute("llm.latency_ms", round(latency_ms, 2))
                self._tracing.set_attribute("llm.input_tokens", input_tokens)
                self._tracing.set_attribute("llm.output_tokens", output_tokens)
                if total_cost > 0:
                    self._tracing.set_attribute("llm.cost_usd", total_cost)
            except Exception:
                pass

        # Step 5: Send trace + generation to Langfuse (Cloud or self-hosted)
        # Creates a Langfuse trace with the same trace_id, and a generation
        # under it with prompt, response, token usage, and cost.
        if self._langfuse is not None:
            try:
                gen_metadata = dict(metadata or {})
                gen_metadata["latency_ms"] = round(latency_ms, 2)
                if total_cost > 0:
                    gen_metadata["cost_usd"] = total_cost

                # Attach correlation/entity context to Langfuse metadata
                _corr_id = ctx.get_correlation_id()
                if _corr_id is not None:
                    gen_metadata["correlation_id"] = _corr_id
                _entity = ctx.get_entity()
                if _entity.get("entity_id") is not None:
                    gen_metadata["entity_id"] = _entity["entity_id"]
                if _entity.get("entity_type") is not None:
                    gen_metadata["entity_type"] = _entity["entity_type"]

                if _LANGFUSE_V4:
                    # Langfuse SDK v3+/v4 API
                    usage_details = {
                        "input": input_tokens,
                        "output": output_tokens,
                        "total": input_tokens + output_tokens,
                    }
                    if total_cost > 0:
                        usage_details["input_cost"] = input_cost
                        usage_details["output_cost"] = output_cost
                        usage_details["total_cost"] = total_cost
                    self._langfuse.start_observation(
                        name=name,
                        as_type="generation",
                        model=model,
                        input=prompt,
                        output=output_text,
                        metadata=gen_metadata,
                        usage_details=usage_details,
                    ).end()
                else:
                    # Langfuse SDK v2 API
                    usage = {
                        "input": input_tokens,
                        "output": output_tokens,
                        "total": input_tokens + output_tokens,
                    }
                    if total_cost > 0:
                        usage["input_cost"] = input_cost
                        usage["output_cost"] = output_cost
                        usage["total_cost"] = total_cost
                    lf_trace = self._langfuse.trace(
                        name=name,
                        id=trace_id,
                        metadata=gen_metadata,
                    )
                    lf_trace.generation(
                        name=name,
                        model=model,
                        input=prompt,
                        output=output_text,
                        metadata=gen_metadata,
                        usage=usage,
                    )
            except Exception as exc:
                print(f"[obs-sdk] Langfuse generation error: {exc}", file=sys.stderr)

        # Step 6: Record counters and histograms → OTel Collector → Prometheus
        if self._metrics is not None:
            try:
                self._metrics.record_tokens(input_tokens, output_tokens, model)
                self._metrics.record_latency(round(latency_ms, 2), model=model, operation=name)
                if total_cost > 0:
                    self._metrics.record_cost(total_cost, model)
            except Exception:
                pass

        # Step 7: Emit a structured log line → OTel Collector → Loki
        # Includes all LLM metadata as fields so Loki/Grafana can filter on them.
        if self._logging is not None:
            try:
                self._logging._emit(
                    "INFO",
                    f"LLM call completed: {name}",
                    llm="true",
                    llm_model=model,
                    llm_latency_ms=round(latency_ms, 2),
                    llm_input_tokens=input_tokens,
                    llm_output_tokens=output_tokens,
                    llm_cost_usd=total_cost,
                    llm_name=name,
                )
            except Exception:
                pass

        return response

    def score(
        self,
        name: str,
        value: float,
        trace_id: Optional[str] = None,
        comment: str = "",
    ) -> None:
        """Post a quality score for the current (or specified) trace."""
        resolved_trace_id = trace_id or ctx.get_trace_id() or ""

        if not resolved_trace_id:
            print(
                f"[obs-sdk] score('{name}') called with no active trace_id — "
                "score dropped.",
                file=sys.stderr,
            )
            return

        if self._langfuse is not None:
            try:
                if _LANGFUSE_V4:
                    self._langfuse.create_score(
                        trace_id=resolved_trace_id,
                        name=name,
                        value=value,
                        comment=comment or None,
                    )
                else:
                    self._langfuse.score(
                        trace_id=resolved_trace_id,
                        name=name,
                        value=value,
                        comment=comment or None,
                    )
            except Exception as exc:
                print(f"[obs-sdk] Langfuse score error: {exc}", file=sys.stderr)

        if self._logging is not None:
            try:
                self._logging._emit(
                    "INFO",
                    f"LLM score recorded: {name}={value}",
                    llm="true",
                    llm_score_name=name,
                    llm_score=value,
                    llm_score_comment=comment,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        if self._langfuse is not None:
            try:
                self._langfuse.flush()
                if hasattr(self._langfuse, "shutdown"):
                    self._langfuse.shutdown()
            except Exception as exc:
                print(f"[obs-sdk] Langfuse shutdown error: {exc}", file=sys.stderr)
