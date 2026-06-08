"""
Example usage of the de-identification library for observability pipelines.

The engine is loaded ONCE at startup so there is no cold-start delay when
text arrives from traces or spans.

Run from the project root:
    python -m examples.example_usage
"""
from deidentifier import PresidioEngine, PolicyConfig

# Engine loaded once at module startup — all examples share this instance.
# ner_model=None  → regex + Presidio pattern recognizers only (~10 ms/doc, no ML)
# ner_model="gravitee-io/bert-small-pii-detection" → adds NER (~300 ms/doc)
_ENGINE = PresidioEngine.get_instance(ner_model=None)


def example_simple():
    print("=" * 60)
    print("Example 1: LLM input trace — single text")
    print("=" * 60)
    # Represents an LLM input span captured in an observability trace
    text = (
        "User query: Summarize the care plan for John Matthews, "
        "DOB 03/15/1985, SSN 456-78-9012. "
        "Contact: john.matthews@examplecorp.com, +1 (217) 555-9087."
    )
    result = _ENGINE.process(text)
    print(f"Original:      {result.original_text}")
    print(f"De-identified: {result.deidentified_text}")
    print(f"Entities found: {result.entities_processed}\n")


def example_custom_policy():
    print("=" * 60)
    print("Example 2: Tool call output — custom policy (names + emails only)")
    print("=" * 60)
    # Only redact PERSON and EMAIL; leave phone visible for routing purposes
    policy = PolicyConfig.from_dict({
        "default_strategy": "redact",
        "score_threshold": 0.6,
        "entities": {
            "PERSON":        {"strategy": "redact",  "enabled": True},
            "EMAIL_ADDRESS": {"strategy": "redact",  "enabled": True},
            "PHONE_NUMBER":  {"strategy": "redact",  "enabled": False},
            "US_SSN":        {"strategy": "redact",  "enabled": True},
        },
    })
    engine = PresidioEngine(ner_model=None, policy=policy)
    text = (
        "Tool result: Patient Emily Brown, MRN MRN-203948, "
        "at 742 Oak Street. Phone: +1 (217) 555-9087. "
        "Email: emily.brown@clinic.org. SSN: 456-78-9012."
    )
    result = engine.process(text)
    print(f"Original:      {result.original_text}")
    print(f"De-identified: {result.deidentified_text}\n")


def example_batch():
    print("=" * 60)
    print("Example 3: Batch — one trace, multiple span types")
    print("=" * 60)
    # Represents a batch of spans from one observability trace:
    # LLM input, LLM output, knowledge base retrieval, tool call
    texts = [
        # LLM input
        "User: What is the treatment plan for patient Rebecca Langford, "
        "age 58, SSN 456-78-9012?",
        # LLM output
        "Assistant: Rebecca Langford (DOB 11/15/1967) should continue "
        "Metformin 1000 mg. Follow-up: June 10, 2026.",
        # Knowledge base retrieval result
        "KB result: Patient ID RF-203948, insurance policy HLT-9982-AX19, "
        "NPI 1234567890, email rf@hospital.org.",
        # Tool call — no PII (should pass through unchanged)
        "Tool: fetch_lab_results(test='HbA1c', date='2026-06-07')",
    ]
    results = _ENGINE.batch_process(texts)
    labels = ["llm_input", "llm_output", "kb_retrieval", "tool_call"]
    for label, r in zip(labels, results):
        flag = "PII" if r.entities_processed > 0 else "clean"
        print(f"  [{label:<14}] {r.entities_processed} entities ({flag})")
        print(f"    {r.deidentified_text}")
    print()


def example_audit_trail():
    print("=" * 60)
    print("Example 4: Audit trail — entity-level metadata")
    print("=" * 60)
    text = (
        "Embedding input: patient Jane Doe, jane@example.com, "
        "SSN 456-78-9012, MRN MRN-884721."
    )
    result = _ENGINE.process(text)
    print(f"De-identified: {result.deidentified_text}")
    print("Audit entries:")
    for entry in result.audit_record.entries:
        d = entry.to_dict()
        print(
            f"  [{d['entity_type']:<25}] "
            f"pos={d['start']}:{d['end']}  "
            f"strategy={d['strategy']}  "
            f"score={d['score']:.2f}"
        )
    print()


if __name__ == "__main__":
    example_simple()
    example_custom_policy()
    example_batch()
    example_audit_trail()
