"""Named applies-to predicates: `(span) -> bool`.

A spec references one predicate to declare which spans it applies to. Predicates
are deliberately span-shape based (span_type / scope) so a worker needs no
Postgres lookup to decide applicability. They're shared across lenses.

Note on LLM detection: embedding calls carry span_type='embedding', so a plain
`span_type == 'model_call'` already means "an LLM generation call" — no need to
resolve the component_type against the registry.
"""
from __future__ import annotations


def any_span(span):
    return True


def llm_call(span):
    return span.get("span_type") == "model_call"


def queued_op(span):
    # things dispatched through a queue (NATS / task queue / pool) before running
    return span.get("span_type") in ("agent", "workflow")


def orchestrated(span):
    return span.get("span_type") == "workflow"


def retryable(span):
    return span.get("span_type") in (
        "model_call", "tool_call", "retrieval", "embedding", "validation", "agent",
    )


def rate_limited(span):
    return span.get("span_type") in ("model_call", "tool_call")


def batch_op(span):
    return span.get("span_type") in ("validation", "skill_exec")


def levels(span):
    return (span.get("scope") in ("solution", "endpoint", "workflow", "agent")
            or span.get("span_type") in ("solution", "workflow", "agent"))


def sol_wf(span):
    return span.get("span_type") in ("solution", "workflow")


# ---- cost-lens predicates ----
def billable(span):
    """Spans that incur direct monetary cost."""
    return span.get("span_type") in ("model_call", "embedding", "tool_call", "retrieval")


def cost_embedding(span):
    return span.get("span_type") == "embedding"


def cost_tool(span):
    return span.get("span_type") == "tool_call"


def cost_kb(span):
    return span.get("span_type") == "retrieval"


def solution_only(span):
    return span.get("span_type") == "solution"


# ---- quality-lens predicates ----
def retrieval_op(span):
    return span.get("span_type") == "retrieval"


def tool_op(span):
    return span.get("span_type") == "tool_call"


def validated_op(span):
    return span.get("span_type") == "validation"


def data_op(span):
    """Spans that process data records (field/record-level quality applies)."""
    return span.get("span_type") in ("validation", "skill_exec")


def output_bearing(span):
    """Spans whose metadata carries a checkable output payload."""
    return span.get("span_type") in ("model_call", "tool_call", "validation")


def schema_checked(span):
    """Spans that can carry / be checked against an expected schema."""
    return span.get("span_type") in ("model_call", "validation")
