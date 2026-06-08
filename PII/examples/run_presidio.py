"""
Run sample_pii_text.txt through the Presidio de-identification engine.

Usage (from project root):
    python examples/run_presidio.py
"""
import logging
import pathlib

logging.basicConfig(level=logging.INFO, format="%(name)s — %(message)s")
logging.getLogger("presidio-analyzer").setLevel(logging.WARNING)

from deidentifier.presidio.engine import PresidioEngine

SAMPLE_FILE = pathlib.Path(__file__).parent.parent / "tests" / "sample_pii_text.txt"

text = SAMPLE_FILE.read_text(encoding="utf-8")
engine = PresidioEngine.get_instance()
result = engine.process(text)

print(f"Done. {result.entities_processed} entities processed")
for e in result.audit_record.entries:
    d = e.to_dict()
    snippet = text[d["start"]:d["end"]]
    print(f'  [{d["entity_type"]:<25}] score={d["score"]:.2f}  original="{snippet}"')
