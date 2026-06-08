# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install base dependencies
pip install -e .

# Install spaCy model (required for NER-based detection)
python -m spacy download en_core_web_trf

# Install Presidio engine (optional)
pip install presidio-analyzer presidio-anonymizer

# Install dev dependencies
pip install -e ".[dev]"

# Run all tests (spaCy disabled for speed)
pytest

# Run a single test file
pytest tests/test_engine.py

# Run a single test by name
pytest tests/test_engine.py::TestEmailDetection::test_email_removed

# Run with coverage
pytest --cov=deidentifier

# CLI: default engine (regex + spaCy)
python -m deidentifier path/to/file.txt

# CLI: Presidio engine
python -m deidentifier path/to/file.txt --engine presidio

# Start the FastAPI server (Presidio engine, loads once on startup)
uvicorn deidentifier.api:app --host 0.0.0.0 --port 8000 --reload
```

## Architecture

There are **two independent detection engines** that share the same `PolicyConfig`, `AuditLogger`, and `DeidentificationResult` interface:

### Default engine (`deidentifier/engine.py`)
- `DeidentificationEngine` — runs `RegexRecognizer` always, `SpacyRecognizer` optionally.
- Detection pipeline: collect spans from all recognizers → filter by score threshold and policy → `_resolve_overlaps` (keeps highest-scored span on conflict) → apply strategies right-to-left (so earlier character positions stay valid) → log to `AuditLogger`.
- Pass `spacy_model=None` to go regex-only (used in tests to avoid loading the model).
- Use `DeidentificationEngine.get_instance()` in application code to avoid reloading spaCy.

### Presidio engine (`deidentifier/presidio/engine.py`)
- `PresidioEngine` wraps Microsoft Presidio's `AnalyzerEngine` + `AnonymizerEngine`.
- Custom `PatternRecognizer` and `EntityRecognizer` subclasses for entity types not built into Presidio (DATE_OF_BIRTH, MRN, AGE, ZIP_CODE, MEDICAL_LICENSE, US_BANK_NUMBER, MEDICARE_ID, ORG) are registered in `deidentifier/presidio/recognizers.py` via `register_all()`.
- Presidio confidence scores are calibrated lower than the regex recognizer — the engine automatically lowers `score_threshold` to 0.35 when using the default policy.
- Uses `select_pipes(enable=["tok2vec", "ner"])` to cut spaCy inference time ~55%.
- The FastAPI app (`deidentifier/api.py`) exclusively uses `PresidioEngine`.

### Shared components
- **`entities.py`** — `EntityType` enum (21 entity types) and `Strategy` enum (`redact`, `mask`, `replace`).
- **`config.py`** — `PolicyConfig` loaded from YAML (`deidentifier/policies/default.yaml`). Per-entity `strategy` and `enabled` flags override the `default_strategy`. Load custom policy with `PolicyConfig.from_yaml(path)` or `PolicyConfig.from_dict(data)`.
- **`strategies.py`** — Three strategies: `RedactStrategy` → `[ENTITY_TYPE]`, `MaskStrategy` → partial masking with `*`, `ReplaceStrategy` → contextually appropriate Faker-generated fake data. The `_ENTITY_FAKER` dict maps entity types to Faker lambdas.
- **`audit.py`** — `AuditEntry` (per-entity span info) + `AuditRecord` (per-document summary) + `AuditLogger` (in-memory + optional JSONL append-to-file).
- **`pipeline.py`** — `DeidentificationPipeline` is a higher-level wrapper over the default engine that accepts `Document` objects and file paths, and provides `export_audit_log()`.
- **`recognizers/base.py`** — `DetectionResult` dataclass and `BaseRecognizer` ABC (`analyze(text) -> List[DetectionResult]`). All recognizers must subclass this.

### Adding a new entity type
1. Add it to `EntityType` in `entities.py`.
2. Add a regex pattern + score tuple in `RegexRecognizer` (`recognizers/regex_recognizer.py`).
3. Add a `PatternRecognizer` builder and call it in `register_all()` in `presidio/recognizers.py`.
4. Add a Faker lambda to `_ENTITY_FAKER` in `strategies.py` for `replace` strategy support.
5. Add the entity to `_ALL_ENTITIES` in `presidio/engine.py`.
6. Add it to `deidentifier/policies/default.yaml`.

---

## Processing workflow

### Default engine (`DeidentificationEngine.process`)

```
text in
  │
  ├─ RegexRecognizer.analyze(text)       → List[DetectionResult]  (always runs)
  └─ SpacyRecognizer.analyze(text)       → List[DetectionResult]  (if model loaded)
         │
         ▼
  Merge all results into raw list
         │
         ▼
  Filter: score >= policy.score_threshold AND entity enabled in policy
         │
         ▼
  _resolve_overlaps(): sort by start; on tie keep longer/higher-scored span
         │
         ▼
  Sort active spans reverse by start (right-to-left replacement)
  For each span:
    strategy = policy.get_entity_strategy(entity_type)
    replacement = get_strategy(strategy).apply(span_text, entity_type)
    splice replacement into text
    append AuditEntry to AuditRecord
         │
         ▼
  AuditLogger.log(AuditRecord)
         │
         ▼
  DeidentificationResult(document_id, original_text, deidentified_text, audit_record)
```

Right-to-left replacement is critical: substituting from the end first keeps all earlier character offsets valid for subsequent replacements.

### Presidio engine (`PresidioEngine.process`)

```
text in
  │
  ▼
AnalyzerEngine.analyze(text, entities=enabled_entities, language="en")
  → uses spaCy NLP + custom PatternRecognizers
  → returns List[RecognizerResult] with (entity_type, start, end, score)
         │
         ▼
  Filter: score >= policy.score_threshold
         │
         ▼
AnonymizerEngine.anonymize(text, analyzer_results, operators=_operators)
  → _operators built once at init: entity → OperatorConfig from policy strategy
  → Presidio handles span replacement internally
         │
         ▼
  Build AuditRecord from analyzer_results, log, return DeidentificationResult
```

Presidio's anonymizer handles overlap resolution internally; `_resolve_overlaps` is not called in this path.

---

## Entity reference

Detection methods:
- **regex** — `RegexRecognizer` (`recognizers/regex_recognizer.py`), runs in both engines
- **spaCy NER** — `SpacyRecognizer` (default engine) or `SpacyOrgRecognizer` (Presidio engine); model `en_core_web_trf`
- **Presidio built-in** — shipped recognizer in `presidio-analyzer`
- **custom PatternRecognizer** — defined in `presidio/recognizers.py`, registered via `register_all()`

| Entity | Default strategy | Detection method | Score | Key regex / spaCy label |
|---|---|---|---|---|
| PERSON | replace | spaCy NER (`PERSON`), Presidio built-in | 0.85 | spaCy label `PERSON` |
| EMAIL_ADDRESS | redact | regex | 0.95 | `[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}` |
| PHONE_NUMBER | redact | regex, Presidio built-in | 0.85 | NANP: `(?:\+1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}` |
| US_SSN | redact | regex, Presidio built-in | 0.95 | `(?!000\|666)\d{3}[-\s]\d{2}[-\s]\d{4}` |
| CREDIT_CARD | mask | regex, Presidio built-in | 0.90 | Visa/MC/Amex/Discover/JCB/Diners BIN prefixes |
| DATE_TIME | replace | spaCy NER (`DATE`, `TIME`), Presidio built-in | 0.85 | spaCy labels `DATE`, `TIME` |
| DATE_OF_BIRTH | redact | regex (keyword-anchored), custom PatternRecognizer | 0.92 | `(?:DOB\|Date of Birth\|Born\|Birth Date)[:\s]+\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}` |
| LOCATION | replace | spaCy NER (`GPE`, `LOC`, `FAC`), Presidio built-in | 0.85 | spaCy labels `GPE`, `LOC`, `FAC` |
| US_DRIVER_LICENSE | redact | regex (keyword-anchored), Presidio built-in | 0.65 | `driver(?:'s)?\s+license[:\s]+[A-Z]?\d{7,9}` |
| US_PASSPORT | redact | regex, Presidio built-in | 0.70 | `[A-Z][0-9]{8}` (1 letter + 8 digits) |
| IP_ADDRESS | redact | regex, Presidio built-in | 0.92 | IPv4 octet range validated `(?:25[0-5]\|2[0-4]\d\|[01]?\d\d?)` ×4 |
| IBAN_CODE | redact | regex, Presidio built-in | 0.80 | `[A-Z]{2}\d{2}[A-Z0-9]{11,30}` |
| MEDICAL_LICENSE | redact | regex (NPI keyword-anchored), custom PatternRecognizer | 0.88–0.92 | `(?:NPI\|National Provider Identifier)[:\s#]+\d{10}` |
| MEDICAL_RECORD_NUMBER | redact | regex (keyword-anchored), custom PatternRecognizer | 0.88–0.92 | `(?:MRN\|Medical Record\|Patient ID)[:\s#\-]*\d{4,10}` |
| URL | redact | regex, Presidio built-in | 0.90 | `https?://[^\s/$.?#][^\s]*` |
| US_BANK_NUMBER | redact | regex (keyword-anchored), custom PatternRecognizer | 0.80 | `(?:account\|acct\|bank acct?)[:\s]+\d{8,17}` |
| AGE | replace | regex (keyword-anchored), custom PatternRecognizer | 0.80–0.85 | `(?:age[d]?\|years? old)[:\s]+\d{1,3}` or `\d{1,3}[\s\-]+(?:years?\|yrs?)[\s\-]+old` |
| ZIP_CODE | mask | regex (keyword-anchored or ZIP+4), custom PatternRecognizer | 0.90 | `(?:zip code\|postal code)[:\s]+\d{5}(?:-\d{4})?` or bare `\d{5}-\d{4}` |
| NRP | replace | spaCy NER (`ORG` → NRP in default engine) | 0.85 | spaCy label `ORG` (default engine maps ORG→NRP) |
| MEDICARE_ID | redact | custom PatternRecognizer | 0.80–0.95 | MBI format: `[1-9][A-Z][A-Z0-9]{2}-[A-Z0-9]{3}-[A-Z0-9]{4}` |
| ORG | replace | Presidio `SpacyOrgRecognizer` (spaCy `ORG`/`ORGANIZATION`) | 0.85 | spaCy label `ORG` — short all-caps acronyms (≤5 chars) filtered out |

### Context words used by custom PatternRecognizers (Presidio engine only)

Context words boost the score of low-confidence bare-number patterns when these words appear nearby. They have no effect in the default engine.

| Entity | Context words |
|---|---|
| DATE_OF_BIRTH | dob, date of birth, born, birth date, birthdate, birthday, d.o.b, birth, born on, year of birth |
| MEDICAL_RECORD_NUMBER | mrn, medical record, medical record number, patient id, record number, chart, chart number, encounter, case number, file number, registration, visit number, member id, health record, emr, ehr, record # |
| AGE | age, aged, years old, yrs, yr, years of age, patient age, pediatric, geriatric, neonatal, adolescent, child, adult, elderly, infant |
| ZIP_CODE | zip, zip code, zipcode, postal, postal code, post code, mailing code, city, state, address |
| MEDICAL_LICENSE | npi, national provider identifier, national provider, provider id, provider identifier, npi number, prescriber npi, rendering npi, billing npi, ordering npi, referring npi |
| US_BANK_NUMBER | account, account number, acct, routing, routing number, bank account, checking, savings, aba, aba number, wire transfer, direct deposit, bank routing, transit |
| MEDICARE_ID | medicare, beneficiary, mbi, medicare id, beneficiary id, medicare beneficiary, cms, medicare card |

---

## Input ingestion

### Python API

```python
from deidentifier import DeidentificationEngine, DeidentificationPipeline, Document

# 1. Direct engine — returns DeidentificationResult
engine = DeidentificationEngine(spacy_model=None)   # regex-only (fast)
engine = DeidentificationEngine()                    # regex + spaCy
result = engine.process("text here", document_id="optional-id")
results = engine.batch_process(["text1", "text2"], document_ids=["id1", "id2"])

# 2. Pipeline — returns ProcessedDocument (higher-level wrapper)
pipeline = DeidentificationPipeline(audit_log_path="audit.jsonl")

pipeline.process_text("plain string")
pipeline.process_document(Document(id="doc-1", content="...", metadata={"k": "v"}))
pipeline.process_documents([Document(...), Document(...)])
pipeline.process_texts(["text1", "text2"])
pipeline.process_file("path/to/file.txt")           # reads UTF-8, uses filename as document_id

# 3. Presidio engine (same interface)
from deidentifier import PresidioEngine
engine = PresidioEngine.get_instance()
result = engine.process("text here")

# Export audit log after processing
pipeline.export_audit_log("audit_export.json")
records = pipeline.get_audit_records()              # List[dict]
```

### CLI

```bash
# Text output to stdout
python -m deidentifier notes.txt

# JSON output (includes entity list + audit)
python -m deidentifier notes.txt --format json

# Write result to file + save audit log
python -m deidentifier notes.txt --output clean.txt --audit audit.jsonl

# Override all entity strategies
python -m deidentifier notes.txt --strategy mask

# Skip spaCy (regex-only, faster startup)
python -m deidentifier notes.txt --fast

# Use Presidio engine
python -m deidentifier notes.txt --engine presidio

# Custom policy file
python -m deidentifier notes.txt --policy custom_policy.yaml

# Lower confidence threshold
python -m deidentifier notes.txt --score-threshold 0.5
```

### REST API (Presidio engine only)

```
POST /deidentify          { "text": "...", "document_id": "optional" }
POST /deidentify/batch    { "texts": [...], "document_ids": [...] }
POST /deidentify/plain    raw text/plain body (multiline paste in Swagger)
GET  /health
```

---

## Output formats

### `DeidentificationResult` (engine-level)

```python
result.document_id          # str — auto-generated UUID if not provided
result.original_text        # str — unchanged input
result.deidentified_text    # str — text with PHI/PII replaced
result.entities_processed   # int — spans actually replaced (post-filter)
result.audit_record         # AuditRecord
  .entities_found           # int — raw detections before threshold/policy filter
  .entities_processed       # int — after filter
  .entries                  # List[AuditEntry]
    .entity_type            # str e.g. "EMAIL_ADDRESS"
    .strategy               # str e.g. "redact"
    .start, .end            # int character offsets in original_text
    .original_length        # int — len of original span
    .score                  # float — recognizer confidence
    .timestamp              # ISO-8601 UTC string
```

### `ProcessedDocument` (pipeline-level)

Same information, fields renamed: `id`, `original_content`, `deidentified_content`, `metadata` (dict from `Document.metadata`), `entities_found`, `entities_processed`, `audit_entries` (list of dicts via `AuditEntry.to_dict()`).

### CLI `--format json`

```json
{
  "document_id": "...",
  "engine": "default",
  "deidentified_text": "...",
  "entities_found": 5,
  "entities_processed": 4,
  "entries": [{ "entity_type": "EMAIL_ADDRESS", "strategy": "redact", "start": 12, "end": 27, "score": 0.95, ... }]
}
```

### REST API `DeidentifyResponse`

```json
{
  "document_id": "...",
  "deidentified_text": "...",
  "entities_found": 5,
  "entities_processed": 5,
  "processing_time_ms": 328.4,
  "audit_entries": [{ "entity_type": "PERSON", "strategy": "replace", "start": 0, "end": 10, "score": 0.85 }]
}
```

Audit log files (JSONL append during processing, or full JSON export) use `AuditRecord.to_dict()` — one JSON object per line.

---

## Latency

| Engine | Cold start | Per request | Notes |
|---|---|---|---|
| Default (regex-only, `--fast`) | ~0 ms | <5 ms | No model load |
| Default (regex + spaCy) | ~3–5 s | ~50–200 ms | spaCy `en_core_web_trf` loaded once via `_MODEL_CACHE` |
| Presidio | ~7 s | ~330 ms | Pays cold start once; subsequent requests ~0.33 s per API docstring |

- Both engines expose a singleton (`get_instance()`) that loads the spaCy model exactly once per process; repeated instantiation reuses the cached `_MODEL_CACHE` dict.
- The FastAPI server pays the 7 s cold start at `lifespan` startup, not per request.
- In the Presidio engine, `select_pipes(enable=["tok2vec", "ner"])` disables parser/tagger/lemmatizer at runtime, cutting per-call spaCy inference by ~55%.
- Audit logging (in-memory append + optional JSONL write) adds negligible overhead.

---

## Evaluation

```bash
# Evaluate Presidio engine against Kaggle PII dataset (precision/recall/F1 per entity)
python eval/evaluate.py
python eval/evaluate.py --data eval/data/pii_dataset.csv --max-docs 1000 --output eval/output.json
```

Requires the dataset downloaded via `kaggle datasets download alejopaullier/pii-external-dataset -p eval/data --unzip`. Span matching uses IoU ≥ 0.5 as the hit threshold.
