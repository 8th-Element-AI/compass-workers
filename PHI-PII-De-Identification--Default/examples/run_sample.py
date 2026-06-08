"""
Run sample_pii_text.txt through the default de-identification pipeline.

Usage (from project root):
    python examples/run_sample.py
"""
import pathlib
from deidentifier import DeidentificationPipeline

SAMPLE_FILE = pathlib.Path(__file__).parent.parent / "tests" / "sample_pii_text.txt"

pipeline = DeidentificationPipeline(spacy_model="en_core_web_trf")

text = SAMPLE_FILE.read_text(encoding="utf-8")
result = pipeline.process_text(text)

print("=" * 70)
print("DE-IDENTIFIED OUTPUT")
print("=" * 70)
print(result.deidentified_content)

print("\n" + "=" * 70)
print(f"ENTITIES FOUND: {result.entities_processed}")
print("=" * 70)
for entry in result.audit_entries:
    snippet = text[entry["start"]:entry["end"]]
    print(
        f"  [{entry['entity_type']:<25}] "
        f"score={entry['score']:.2f}  "
        f"strategy={entry['strategy']:<7}  "
        f'original="{snippet}"'
    )
