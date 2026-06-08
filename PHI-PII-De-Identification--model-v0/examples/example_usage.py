"""
Example usage of the de-identification library.

Run from the project root:
    python -m examples.example_usage
"""
from deidentifier import DeidentificationPipeline, PolicyConfig
from deidentifier.pipeline import Document


def example_simple():
    print("=" * 60)
    print("Example 1: Simple Text De-identification")
    print("=" * 60)
    pipeline = DeidentificationPipeline(spacy_model="en_core_web_trf")
    text = (
        "Patient John Smith (DOB: 03/15/1985) can be reached at "
        "john.smith@email.com or (555) 123-4567. SSN: 123-45-6789."
    )
    result = pipeline.process_text(text)
    print(f"Original:\n  {result.original_content}")
    print(f"De-identified:\n  {result.deidentified_content}")
    print(f"Entities processed: {result.entities_processed}\n")


def example_custom_policy():
    print("=" * 60)
    print("Example 2: Custom Policy (replace names, mask phones)")
    print("=" * 60)
    policy = PolicyConfig.from_dict(
        {
            "default_strategy": "redact",
            "score_threshold": 0.6,
            "entities": {
                "PERSON": {"strategy": "redact", "enabled": True},
                "EMAIL_ADDRESS": {"strategy": "redact", "enabled": True},
                "PHONE_NUMBER": {"strategy": "mask", "enabled": True},
                "US_SSN": {"strategy": "redact", "enabled": True},
            },
        }
    )
    pipeline = DeidentificationPipeline(policy=policy, spacy_model="en_core_web_trf")
    text = "Dr. Alice Brown: alice.brown@hospital.com, (800) 555-1234. SSN: 987-65-4321."
    result = pipeline.process_text(text)
    print(f"Original:\n  {result.original_content}")
    print(f"De-identified:\n  {result.deidentified_content}\n")


def example_batch():
    print("=" * 60)
    print("Example 3: Batch Document Processing")
    print("=" * 60)
    pipeline = DeidentificationPipeline(spacy_model="en_core_web_trf")
    documents = [
        Document(id="note-001", content="SSN: 111-22-3333, credit card 4532015112830366"),
        Document(id="note-002", content="Email: bob@example.com, MRN: 987654321"),
        Document(id="note-003", content="Test result is negative. No action needed."),
    ]
    results = pipeline.process_documents(documents)
    for r in results:
        print(f"  [{r.id}] {r.entities_processed} entities | {r.deidentified_content}")
    print()


def example_audit_trail():
    print("=" * 60)
    print("Example 4: Audit Trail")
    print("=" * 60)
    pipeline = DeidentificationPipeline(
        audit_log_path="audit.jsonl",
        spacy_model="en_core_web_trf",
    )
    text = "Patient: Jane Doe | Email: jane@example.com | SSN: 987-65-4321"
    result = pipeline.process_text(text)
    print(f"De-identified:\n  {result.deidentified_content}")
    print("Audit entries:")
    for entry in result.audit_entries:
        print(
            f"  [{entry['entity_type']}] "
            f"pos={entry['start']}:{entry['end']} "
            f"strategy={entry['strategy']} "
            f"score={entry['score']:.2f}"
        )
    print()


if __name__ == "__main__":
    example_simple()
    example_custom_policy()
    example_batch()
    example_audit_trail()
