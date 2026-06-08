# Deidentifier

A PHI/PII de-identification library built on [Microsoft Presidio](https://github.com/microsoft/presidio). Designed for observability pipelines that process LLM traces and spans — plain text in, plain text out, no cold-start delay when text arrives.

## Table of contents

- [Overview](#overview)
- [Installation](#installation)
- [NER model options](#ner-model-options)
- [Quick start](#quick-start)
- [Python API](#python-api)
- [CLI](#cli)
- [REST API](#rest-api)
- [Entities detected](#entities-detected)
- [De-identification strategies](#de-identification-strategies)
- [Policy configuration](#policy-configuration)
- [Audit trail](#audit-trail)
- [Performance](#performance)
- [Testing](#testing)
- [Evaluation](#evaluation)
- [Project structure](#project-structure)

---

## Overview

The engine detects 21 PHI/PII entity types using a combination of:

- **Regex patterns** — high-precision rules for structured data (SSN, email, phone, credit card, IP address, MRN, NPI, IBAN, …)
- **Presidio built-in recognizers** — pattern + context recognizers shipped with Presidio
- **Custom PatternRecognizers** — project-specific recognizers for DATE_OF_BIRTH, MRN, AGE, ZIP_CODE, MEDICAL_LICENSE, US_BANK_NUMBER, MEDICARE_ID, ORG
- **NER model** *(optional)* — a spaCy or HuggingFace transformer model for PERSON, LOCATION, DATE_TIME, and related entities

The `ner_model` parameter selects the NER backend; everything else stays the same.

---

## Installation

```bash
# Core dependencies
pip install -e .

# Download the default spaCy tokenizer (required)
python -m spacy download en_core_web_sm

# Optional: spaCy large model for better spaCy-based NER
python -m spacy download en_core_web_lg

# Optional: HuggingFace models (cache before first use)
python -c "from transformers import pipeline; pipeline('ner', model='gravitee-io/bert-small-pii-detection', aggregation_strategy='first')"
python -c "from transformers import pipeline; pipeline('ner', model='dslim/bert-base-NER', aggregation_strategy='first')"

# Dev / testing
pip install -e ".[dev]"
```

---

## NER model options

`ner_model` controls which model runs entity recognition. Pass it to `PresidioEngine()` or `PresidioEngine.get_instance()`.

| `ner_model` value | NER backend | Latency | When to use |
|---|---|---|---|
| `None` | No NLP model — regex + Presidio pattern recognizers only | < 15 ms | Fastest; structured PII only |
| `"en_core_web_lg"` | spaCy large model | ~50–100 ms | Good NER without GPU |
| `"gravitee-io/bert-small-pii-detection"` **(default)** | HuggingFace BERT (110 MB) | ~300 ms | Best coverage for PII/PHI |
| `"dslim/distilbert-NER"` | HuggingFace DistilBERT (250 MB) | ~150 ms | Lighter transformer option |
| `"dslim/bert-base-NER"` | HuggingFace BERT-base (413 MB) | ~300 ms | CoNLL-2003 NER schema |
| `"iiiorg/piiranha-v1-detect-personal-information"` | HuggingFace (1.1 GB) | ~400 ms | Broad PII coverage |
| `"obi/deid_roberta_i2b2"` | HuggingFace RoBERTa (1.4 GB) | ~500 ms | Clinical PHI, i2b2 schema |

When a HuggingFace model is used, `en_core_web_sm` is used internally for tokenization only (character offset tracking). The NER is done entirely by the HF model.

**Models must be cached locally before the engine starts — the engine never downloads at runtime.**

---

## Quick start

```python
from deidentifier import PresidioEngine

# Load once at startup — no cold-start on subsequent calls
engine = PresidioEngine.get_instance()   # default: gravitee-io/bert-small-pii-detection

result = engine.process("Patient John Smith, SSN 456-78-9012, called from (217) 555-9087.")
print(result.deidentified_text)
# → Patient <PERSON>, SSN [US_SSN], called from [PHONE_NUMBER].
```

---

## Python API

### Load the engine

```python
from deidentifier import PresidioEngine

# Default — gravitee-io HuggingFace model
engine = PresidioEngine.get_instance()

# Regex-only (no ML model, fastest)
engine = PresidioEngine.get_instance(ner_model=None)

# spaCy large model for NER
engine = PresidioEngine.get_instance(ner_model="en_core_web_lg")

# Specific HuggingFace model
engine = PresidioEngine.get_instance(ner_model="dslim/bert-base-NER")
```

`get_instance()` returns a singleton — the model is loaded exactly once per process regardless of how many times you call it.

### Process text

```python
# Single document
result = engine.process("Jane Doe, jane@example.com, DOB 03/15/1985")
result.deidentified_text    # str  — text with PHI replaced
result.entities_processed   # int  — number of entities replaced
result.audit_record         # AuditRecord — full entity metadata

# Batch
results = engine.batch_process(["text1", "text2", "text3"])
```

### DeidentificationResult fields

```python
result.document_id          # str — auto-generated UUID if not provided
result.original_text        # str — unchanged input
result.deidentified_text    # str — text with PHI/PII replaced
result.entities_processed   # int — spans replaced (post-filter)
result.audit_record
  .entities_found           # int — raw detections before threshold/policy filter
  .entities_processed       # int — after filter
  .entries                  # List[AuditEntry]
    .entity_type            # str  e.g. "EMAIL_ADDRESS"
    .strategy               # str  e.g. "redact"
    .start, .end            # int  character offsets in original_text
    .score                  # float — recognizer confidence
    .timestamp              # ISO-8601 UTC string
```

### Custom policy

```python
from deidentifier import PresidioEngine, PolicyConfig

policy = PolicyConfig.from_dict({
    "default_strategy": "redact",
    "score_threshold": 0.6,
    "entities": {
        "PERSON":        {"strategy": "redact",  "enabled": True},
        "EMAIL_ADDRESS": {"strategy": "redact",  "enabled": True},
        "PHONE_NUMBER":  {"strategy": "redact",  "enabled": False},  # leave phone as-is
    },
})
engine = PresidioEngine(ner_model=None, policy=policy)
result = engine.process("Jane Doe, jane@example.com, +1 (217) 555-9087.")
```

### Observability usage pattern

For observability pipelines (LLM traces, spans) you typically only need to know
**whether** PII is present, **how many** entities, and **which types** — not a
de-identified copy of the text. Use `analyze()` / `batch_analyze()` for this:
they run only the detection step and skip anonymization entirely, so they are
faster than `process()`.

```python
from deidentifier import PresidioEngine

# Load once at service startup
_engine = PresidioEngine.get_instance()

# Single span
result = _engine.analyze("Patient John Smith, SSN 456-78-9012, called from (217) 555-9087.")
result.has_pii       # True
result.entity_count  # 3
result.entities      # {"PERSON": 1, "US_SSN": 1, "PHONE_NUMBER": 1}

# Batch of spans from one trace
texts = [
    "User: Summarize the care plan for Rebecca Langford, DOB 11/15/1967.",
    "Tool: fetch_lab_results(test='HbA1c', date='2026-06-07')",   # no PII
    "KB result: Patient ID RF-203948, email rf@hospital.org.",
]
results = _engine.batch_analyze(texts)
for r in results:
    print(r.has_pii, r.entity_count, r.entities)
# True   2  {"PERSON": 1, "DATE_OF_BIRTH": 1}
# False  0  {}
# True   2  {"MEDICAL_RECORD_NUMBER": 1, "EMAIL_ADDRESS": 1}
```

`AnalysisResult` fields:

| Field | Type | Description |
|---|---|---|
| `has_pii` | `bool` | `True` if any entity was detected |
| `entity_count` | `int` | Total number of detected entities |
| `entities` | `Dict[str, int]` | Entity type → occurrence count |

---

## CLI

```bash
# De-identify a file (output to stdout)
python -m deidentifier notes.txt

# JSON output (includes entity list + audit)
python -m deidentifier notes.txt --format json

# Write result to file + save audit log
python -m deidentifier notes.txt --output clean.txt --audit audit.jsonl

# Override all entity strategies
python -m deidentifier notes.txt --strategy mask

# Use a specific NER model
python -m deidentifier notes.txt --ner-model gravitee-io/bert-small-pii-detection
python -m deidentifier notes.txt --ner-model en_core_web_lg

# Regex-only (no NER model, fastest)
python -m deidentifier notes.txt --ner-model none

# Custom policy file
python -m deidentifier notes.txt --policy custom_policy.yaml

# Lower confidence threshold
python -m deidentifier notes.txt --score-threshold 0.5
```

**CLI flags:**

| Flag | Default | Description |
|---|---|---|
| `--ner-model MODEL` | `None` (regex-only) | HuggingFace model ID or spaCy model name |
| `--policy YAML` | built-in default | Path to custom policy YAML |
| `--strategy` | from policy | Override all entities: `redact`, `mask`, or `replace` |
| `--score-threshold FLOAT` | from policy | Minimum confidence score 0.0–1.0 |
| `--format` | `text` | Output format: `text` or `json` |
| `--output FILE` | stdout | Write de-identified text to file |
| `--audit FILE` | none | Write audit log (JSONL) to file |

---

## REST API

Start the server (loads engine once, no per-request cold start):

```bash
uvicorn deidentifier.api:app --host 0.0.0.0 --port 8000

# Use a different NER model
DEIDENTIFIER_NER_MODEL=en_core_web_lg uvicorn deidentifier.api:app --port 8000

# Regex-only (set env var to empty string)
DEIDENTIFIER_NER_MODEL= uvicorn deidentifier.api:app --port 8000
```

**Endpoints:**

```
GET  /health
POST /deidentify          { "text": "...", "document_id": "optional" }
POST /deidentify/batch    { "texts": [...], "document_ids": [...] }
POST /deidentify/plain    raw text/plain body
```

**`POST /deidentify` response:**

```json
{
  "document_id": "abc-123",
  "deidentified_text": "Patient <PERSON> called from [PHONE_NUMBER].",
  "entities_found": 2,
  "entities_processed": 2,
  "processing_time_ms": 312.4,
  "audit_entries": [
    { "entity_type": "PERSON",       "strategy": "replace", "start": 8,  "end": 18, "score": 0.85 },
    { "entity_type": "PHONE_NUMBER", "strategy": "redact",  "start": 31, "end": 45, "score": 0.85 }
  ]
}
```

Interactive docs available at `http://localhost:8000/docs`.

---

## Entities detected

| Entity | Default strategy | Detection method |
|---|---|---|
| `PERSON` | replace | spaCy / HuggingFace NER |
| `EMAIL_ADDRESS` | redact | regex |
| `PHONE_NUMBER` | redact | regex, Presidio built-in |
| `US_SSN` | redact | regex, Presidio built-in |
| `CREDIT_CARD` | mask | regex, Presidio built-in |
| `DATE_TIME` | replace | spaCy / HuggingFace NER, Presidio built-in |
| `DATE_OF_BIRTH` | redact | regex (keyword-anchored), custom PatternRecognizer |
| `LOCATION` | replace | spaCy / HuggingFace NER, Presidio built-in |
| `US_DRIVER_LICENSE` | redact | regex (keyword-anchored), Presidio built-in |
| `US_PASSPORT` | redact | regex, Presidio built-in |
| `IP_ADDRESS` | redact | regex, Presidio built-in |
| `IBAN_CODE` | redact | regex, Presidio built-in |
| `MEDICAL_LICENSE` | redact | regex (NPI keyword-anchored), custom PatternRecognizer |
| `MEDICAL_RECORD_NUMBER` | redact | regex (keyword-anchored), custom PatternRecognizer |
| `URL` | redact | regex, Presidio built-in |
| `US_BANK_NUMBER` | redact | regex (keyword-anchored), custom PatternRecognizer |
| `AGE` | replace | regex (keyword-anchored), custom PatternRecognizer |
| `ZIP_CODE` | mask | regex (keyword-anchored or ZIP+4), custom PatternRecognizer |
| `NRP` | replace | spaCy NER |
| `MEDICARE_ID` | redact | custom PatternRecognizer (MBI format) |
| `ORG` | replace | spaCy / HuggingFace NER |

---

## De-identification strategies

| Strategy | Output | Example |
|---|---|---|
| `redact` | `[ENTITY_TYPE]` | `[EMAIL_ADDRESS]` |
| `mask` | `****` (same length) | `****@*********` |
| `replace` | Synthetic Faker data | `Emily Johnson`, `Springfield, IL` |

Strategies are configured per entity type in the policy. You can also override all entities at once with `--strategy` (CLI) or a `PolicyConfig`.

---

## Policy configuration

The default policy is at `deidentifier/policies/default.yaml`. Load a custom one:

```python
policy = PolicyConfig.from_yaml("my_policy.yaml")
policy = PolicyConfig.from_dict({...})
```

**Policy YAML structure:**

```yaml
version: "1.0"
default_strategy: redact
score_threshold: 0.7

entities:
  PERSON:
    strategy: replace
    enabled: true
  EMAIL_ADDRESS:
    strategy: redact
    enabled: true
  PHONE_NUMBER:
    strategy: redact
    enabled: false   # disable to leave phone numbers unchanged
```

`score_threshold` filters detections below a confidence level. When no policy is provided to `PresidioEngine`, the threshold is automatically lowered to `0.35` to accommodate Presidio's lower base scores (context-boosted scores still exceed this).

---

## Audit trail

Every `process()` call produces an `AuditRecord` attached to the result:

```python
result = engine.process(text, document_id="trace-001")
for entry in result.audit_record.entries:
    d = entry.to_dict()
    print(d["entity_type"], d["start"], d["end"], d["score"], d["strategy"])
```

Write to JSONL for persistent logging (append-only, one record per line):

```python
from deidentifier import AuditLogger, PresidioEngine

logger = AuditLogger(log_path="audit.jsonl")
engine = PresidioEngine(ner_model=None, audit_logger=logger)
engine.process("Jane Doe, SSN 456-78-9012")
# → appended to audit.jsonl
```

---

## Performance

| Mode | Cold start | Per request |
|---|---|---|
| Regex-only (`ner_model=None`) | ~0 ms | < 15 ms |
| spaCy large (`ner_model="en_core_web_lg"`) | ~2 s | ~50–100 ms |
| HuggingFace default (`gravitee-io/bert-small-pii-detection`) | ~5 s | ~300 ms |
| HuggingFace large (`obi/deid_roberta_i2b2`) | ~10 s | ~500 ms |

Cold start happens **once** per process via `get_instance()`. The FastAPI server pays it at startup, not per request.

---

## Testing

```bash
# Run all tests (regex-only, no model download needed)
pytest

# Single test file
pytest tests/test_presidio_engine.py

# Single test
pytest tests/test_presidio_engine.py::TestReturnType::test_returns_deidentification_result

# With coverage
pytest --cov=deidentifier
```

Tests use `ner_model=None` throughout so they run without GPU or large model downloads.

---

## Evaluation

Evaluate against the [Kaggle PII external dataset](https://www.kaggle.com/datasets/alejopaullier/pii-external-dataset) (precision / recall / F1 per entity type):

```bash
# Download dataset
kaggle datasets download alejopaullier/pii-external-dataset -p eval/data --unzip

# Run evaluation (default: first 500 documents)
python eval/evaluate.py

# Full run
python eval/evaluate.py --data eval/data/pii_dataset.csv --max-docs 5000 --output eval/output.json
```

Span matching uses IoU ≥ 0.5 as the hit threshold.

---

## Project structure

```
deidentifier/
├── __init__.py                  # Public exports
├── entities.py                  # EntityType and Strategy enums
├── config.py                    # PolicyConfig (YAML / dict loading)
├── strategies.py                # RedactStrategy, MaskStrategy, ReplaceStrategy
├── audit.py                     # AuditEntry, AuditRecord, AuditLogger
├── result.py                    # DeidentificationResult dataclass
├── presidio/
│   ├── engine.py                # PresidioEngine — the single engine
│   └── recognizers.py           # Custom PatternRecognizers + register_all()
├── recognizers/
│   ├── base.py                  # BaseRecognizer ABC, DetectionResult
│   └── regex_recognizer.py      # RegexRecognizer
├── policies/
│   └── default.yaml             # Default policy (21 entities)
├── api.py                       # FastAPI REST service
├── cli.py                       # Command-line interface
└── __main__.py                  # python -m deidentifier entry point

examples/
├── example_usage.py             # Observability pipeline examples
└── run_presidio.py              # Simple Presidio engine demo

tests/
├── test_presidio_engine.py
├── test_recognizers.py
├── test_strategies.py
├── test_cli.py
└── fixtures.py

eval/
└── evaluate.py                  # Precision/recall/F1 against Kaggle dataset
```

### Adding a new entity type

1. Add it to `EntityType` in `entities.py`
2. Add a regex pattern in `RegexRecognizer` (`recognizers/regex_recognizer.py`)
3. Add a `PatternRecognizer` and register it in `presidio/recognizers.py`
4. Add a Faker lambda to `_ENTITY_FAKER` in `strategies.py` (for `replace` strategy)
5. Add it to `_ALL_ENTITIES` in `presidio/engine.py`
6. Add it to `deidentifier/policies/default.yaml`
