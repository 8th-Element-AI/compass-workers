# PII Detection & De-Identification

Presidio-backed PII / PHI detection and redaction for the Signal platform.
Used by the Safety lens worker for the `pii_count` and `pii_detected` metrics;
also usable standalone as a CLI, library, or REST API.

```
text ──► PresidioEngine.analyze()  ──►  AnalysisResult  (entity counts, types — no text returned)
     ──► PresidioEngine.process()  ──►  DeidentificationResult  (anonymized text + audit log)
```

**Three detection modes**, picked at construction time. Same API; different
recall/latency tradeoffs.

| Mode | Detector | Latency / doc (CPU) | Recall (PERSON/LOC/DATE) |
|---|---|---|---|
| Regex-only | Presidio pattern recognizers | <15 ms | low (catches structured PII: emails, SSNs, cards) |
| spaCy | `en_core_web_sm`/`lg` + Presidio | ~50 ms | medium |
| HuggingFace NER | e.g. `gravitee-io/bert-small-pii-detection` | ~330 ms | high |

Models must be downloaded **before** the engine starts. No network calls happen
during text processing.

---

## Table of contents

1. [What's in this repo](#1-whats-in-this-repo)
2. [Installation](#2-installation)
3. [Quick start (Python)](#3-quick-start-python)
4. [Command-line interface](#4-command-line-interface)
5. [REST API](#5-rest-api)
6. [Entity types & policy](#6-entity-types--policy)
7. [Batch processing](#7-batch-processing)
8. [Audit logging](#8-audit-logging)
9. [Models — what to pick and why](#9-models--what-to-pick-and-why)
10. [Performance & benchmarks](#10-performance--benchmarks)
11. [Use inside Signal Workers](#11-use-inside-signal-workers)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. What's in this repo

```
PII/
├── pyproject.toml
├── requirements.txt
├── README.md                    (this file)
├── deidentifier/
│   ├── __main__.py              python -m deidentifier
│   ├── cli.py                   CLI entrypoint (`deidentify` script)
│   ├── api.py                   FastAPI server (deidentifier-api)
│   ├── audit.py                 AuditLogger + AuditRecord/AuditEntry
│   ├── config.py                PolicyConfig (YAML-loadable)
│   ├── entities.py              Strategy enum (redact | mask | replace)
│   ├── result.py                AnalysisResult, DeidentificationResult
│   ├── policies/
│   │   └── default.yaml         Built-in default policy
│   ├── presidio/
│   │   ├── engine.py            PresidioEngine — singleton + the analyze/process API
│   │   └── recognizers.py       Custom regex recognizers (medical IDs, ZIP, etc.)
│   └── recognizers/
│       ├── base.py              BaseRecognizer ABC + DetectionResult
│       └── regex_recognizer.py  RegexRecognizer
└── eval/
    ├── evaluate.py              IOU-based eval against gold dataset
    ├── benchmark_models.py      Compare 10 different NER models
    └── BENCHMARK_RESULTS.md     Recent benchmark numbers
```

---

## 2. Installation

```bash
pip install -e .
python -m spacy download en_core_web_sm   # always required (used as the tokenizer)
```

**For HuggingFace NER models** (any model id with a `/` in it like
`gravitee-io/bert-small-pii-detection`), pre-cache it once before starting the
engine:

```bash
python -c "from transformers import pipeline; \
           pipeline('ner', model='gravitee-io/bert-small-pii-detection', \
                    aggregation_strategy='first')"
```

The engine refuses to download at runtime by design — no surprise network calls
in production pods. If the model isn't in the HF cache when `PresidioEngine`
starts, it raises `RuntimeError` with a hint pointing at the command above.

**For the larger spaCy model** (only if you'll use `ner_model="en_core_web_lg"`):

```bash
python -m spacy download en_core_web_lg
```

---

## 3. Quick start (Python)

```python
from deidentifier import PresidioEngine

# Fastest: regex + Presidio's built-in pattern recognizers, no ML
engine = PresidioEngine(ner_model=None)

# Or with HuggingFace NER for high recall on PERSON / LOCATION / DATE_TIME
engine = PresidioEngine(ner_model="gravitee-io/bert-small-pii-detection")

# Detect only (used by observability — no text returned)
result = engine.analyze("Patient John Smith was born 12/3/1980.")
print(result.has_pii, result.entity_count, result.entities)
# True, 3, {'PERSON': 1, 'DATE_TIME': 1, 'DATE_OF_BIRTH': 1}

# Full de-identification (replace strategy default for PERSON, redact for others)
result = engine.process("Patient John Smith was born 12/3/1980.")
print(result.deidentified_text)
# "Patient <PERSON> was born <DATE_OF_BIRTH>."
```

For workloads with many texts, use the **concurrent** batch path — it's the
recommended entry point and what the Signal Safety worker uses:

```python
texts = ["Email me at jane@x.com", "Phone: (555) 123-4567", "SSN 123-45-6789"]
results = engine.analyze_batch(texts, batch_size=4)
for r in results:
    print(r.has_pii, r.entity_count, r.entities)
```

### Singleton pattern

For long-lived services, use the thread-safe singleton — the engine loads
analyzers once at first call and reuses them:

```python
engine = PresidioEngine.get_instance(ner_model="gravitee-io/bert-small-pii-detection")
```

Subsequent calls with a different `ner_model` log a warning and return the
existing instance. Use `PresidioEngine.reset_singleton()` in tests.

---

## 4. Command-line interface

After install you get the `deidentify` script (and `python -m deidentifier`):

```bash
# Regex-only (no ML, instant startup)
deidentify notes.txt

# With HuggingFace NER
deidentify notes.txt --ner-model gravitee-io/bert-small-pii-detection

# Write output to file + audit log alongside
deidentify notes.txt --output clean.txt --audit audit.jsonl

# Override strategy globally for all entity types
deidentify notes.txt --strategy mask

# JSON output (includes per-entity details)
deidentify notes.txt --format json

# Custom policy
deidentify notes.txt --policy ./my-policy.yaml
```

`--help` prints the full option list.

---

## 5. REST API

`deidentifier/api.py` is a FastAPI server suitable for sidecar deployment.

```bash
pip install -e ".[api]"   # or just: pip install fastapi uvicorn
uvicorn deidentifier.api:app --host 0.0.0.0 --port 8000
```

Endpoints:

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/health` | — | `{status, ner_model, ready}` |
| POST | `/deidentify` | `{text, document_id?}` | `DeidentifyResponse` (anonymized + audit) |
| POST | `/deidentify/batch` | `{texts, document_ids?}` | `BatchResponse` |

Configure the NER model via env:

```bash
DEIDENTIFIER_NER_MODEL=gravitee-io/bert-small-pii-detection
```

The server uses the singleton, so model load happens once on first request.

---

## 6. Entity types & policy

### Supported entity types

21 types out of the box (Presidio built-ins + custom regex recognizers):

```
CREDIT_CARD, DATE_TIME, EMAIL_ADDRESS, IBAN_CODE, IP_ADDRESS, LOCATION,
MEDICAL_LICENSE, PERSON, PHONE_NUMBER, URL, NRP, US_BANK_NUMBER,
US_DRIVER_LICENSE, US_PASSPORT, US_SSN, DATE_OF_BIRTH, MEDICAL_RECORD_NUMBER,
AGE, ZIP_CODE, MEDICARE_ID, ORG
```

### Three strategies

| Strategy | Output for "John Smith" |
|---|---|
| `redact` | `<PERSON>` |
| `mask` | `**********` |
| `replace` | `Robert Johnson` (Faker-generated, deterministic per text) |

### Policy YAML

Default policy lives at `deidentifier/policies/default.yaml`. Override per
entity:

```yaml
version: "1.0"
default_strategy: redact
score_threshold: 0.7        # drop detections below this confidence
terms: []                   # additional literal terms to match

entities:
  PERSON:
    strategy: replace
    enabled: true
  EMAIL_ADDRESS:
    strategy: redact
    enabled: true
  CREDIT_CARD:
    strategy: mask
    enabled: true
  # entities not listed inherit default_strategy
```

Use it:

```python
from deidentifier.config import PolicyConfig
policy = PolicyConfig.from_yaml("./my-policy.yaml")
engine = PresidioEngine(policy=policy)
```

Or via CLI: `deidentify input.txt --policy ./my-policy.yaml`.

---

## 7. Batch processing

For >5 texts at once, **always use `analyze_batch`** — it parallelizes across
texts with a `ThreadPoolExecutor` and gives you ~`batch_size`× speedup on a
multi-core CPU. The other batch methods are kept for backwards-compatibility:

| Method | Concurrency | Returns | Recommended |
|---|---|---|---|
| `engine.analyze_batch(texts, batch_size=4)` | parallel (threadpool) | `list[AnalysisResult]` | ✅ |
| `engine.batch_analyze(texts)` | sequential | `list[AnalysisResult]` | only if concurrency is unsafe in caller |
| `engine.batch_process(texts)` | sequential | `list[DeidentificationResult]` | use only when you need full anonymization |

Sane defaults: `batch_size=4` on a 4-core box, scale up linearly with cores up
to ~16. Above that, Presidio's per-text overhead dominates and concurrency
plateaus.

```python
texts = [open(f).read() for f in glob("notes/*.txt")]
results = engine.analyze_batch(texts, batch_size=8)

flagged = [(i, r) for i, r in enumerate(results) if r.has_pii]
```

---

## 8. Audit logging

Every `process()` call produces an `AuditRecord` describing what was found and
how it was transformed. The default `AuditLogger` is in-memory; pass a path to
persist as JSONL:

```python
from deidentifier import PresidioEngine
from deidentifier.audit import AuditLogger

engine = PresidioEngine(audit_logger=AuditLogger(log_path="./audit.jsonl"))
engine.process("Patient John Smith, SSN 123-45-6789")
# audit.jsonl now contains a JSON line with the per-entity detail
```

Each line of audit.jsonl looks like:

```json
{"document_id": "doc-123", "entities_found": 2, "entities_processed": 2,
 "entries": [
   {"entity_type": "PERSON", "strategy": "replace", "start": 8, "end": 18, "score": 0.95},
   {"entity_type": "US_SSN", "strategy": "redact", "start": 25, "end": 36, "score": 0.99}
 ]}
```

Notes:

- **Original PII never lands in the audit log** — only entity type, position, length, score, and the strategy applied.
- Safe to ship audit logs out of the secure environment.

---

## 9. Models — what to pick and why

| Goal | Recommended `ner_model` | Latency | Recall | Notes |
|---|---|---|---|---|
| Compliance scans on structured docs (cards, IDs, emails) | `None` | <15 ms | low for free-text PII | Regex-only is enough; the patterns catch all the structured PII formats |
| General observability, no name catch needed | `en_core_web_sm` | ~50 ms | medium | spaCy small. Cheap. |
| Production observability with name/location catch | `gravitee-io/bert-small-pii-detection` | ~330 ms | high | What the Signal Safety worker uses |
| Medical (clinical notes) | `obi/deid_roberta_i2b2` | ~600 ms | very high (medical) | Specifically trained on i2b2 |
| Highest recall, any domain | `iiiorg/piiranha-v1-detect-personal-information` | ~500 ms | very high | Largest of the recommended models |

See `eval/BENCHMARK_RESULTS.md` for F1 scores on a 500-doc evaluation set across
all 10 benchmarked models.

### Why we default to `gravitee-io/bert-small-pii-detection` in Signal

It hits the best F1 for the cost: ~85% F1 at 330 ms/doc on CPU. The
`gravitee-io` variant is small enough to be pre-cached in the Safety Docker
image (~220 MB) without ballooning the image size.

---

## 10. Performance & benchmarks

### Single-document latency (Intel x86-64, CPU only)

| Detector | p50 | p95 |
|---|---|---|
| regex-only | 8 ms | 15 ms |
| `en_core_web_sm` | 40 ms | 70 ms |
| `gravitee-io/bert-small-pii-detection` | 280 ms | 450 ms |
| `obi/deid_roberta_i2b2` | 520 ms | 800 ms |

### Throughput with `analyze_batch(batch_size=N)`

Linear-ish up to physical core count, then plateaus.

| batch_size | docs/sec (HF NER, 4-core machine) |
|---|---|
| 1 | 3 |
| 4 | 11 |
| 8 | 18 |
| 16 | 21 |

The Safety worker uses `batch_size=4` by default (`SIGNAL_PII_BATCH=4`).

### Running the benchmark yourself

```bash
pip install kaggle
kaggle datasets download alejopaullier/pii-external-dataset -p eval/data --unzip

# Single model
python eval/evaluate.py --max-docs 500

# All 10 models, side by side
python eval/benchmark_models.py --max-docs 500
```

---

## 11. Use inside Signal Workers

The Safety lens worker imports this package as `deidentifier`:

```python
from deidentifier import PresidioEngine

engine = PresidioEngine.get_instance(ner_model=os.environ["SIGNAL_PII_NER_MODEL"])
results = engine.analyze_batch(unique_texts, batch_size=int(os.environ["SIGNAL_PII_BATCH"]))
```

Relevant Signal env vars:

| Env var | Default | What it does |
|---|---|---|
| `SIGNAL_PII_NER_MODEL` | `gravitee-io/bert-small-pii-detection` | Picks the NER model |
| `SIGNAL_PII_BATCH` | `4` | ThreadPool width for analyze_batch |
| `SIGNAL_PII_CACHE_MAX` | `20000` | Per-worker LRU on content hashes |

The Safety Docker image pre-caches the default model during build:

```dockerfile
RUN python -c "from transformers import pipeline; \
    pipeline('ner', model='gravitee-io/bert-small-pii-detection', \
             aggregation_strategy='first')"
```

So no runtime download is ever needed.

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImportError: Presidio packages are not installed` | `presidio-analyzer` / `presidio-anonymizer` missing | `pip install presidio-analyzer presidio-anonymizer` (already in `requirements.txt`) |
| `OSError: [E050] Can't find model 'en_core_web_sm'` | spaCy model not installed | `python -m spacy download en_core_web_sm` |
| `RuntimeError: Model '...' is not cached locally` | HF NER model not pre-cached | `python -c "from transformers import pipeline; pipeline('ner', model='...', aggregation_strategy='first')"` |
| Engine returns `entity_count=0` but PII is clearly present | `score_threshold` too high in policy | Lower it (e.g. `0.35` for HF NER models) |
| Detection results have low scores on names | Using regex-only mode | Switch to spaCy or HF NER |
| Process hangs on first call | First-time model load — can take 5-10s on CPU | Use the singleton (`get_instance()`) and warm it during startup |
| Audit log shows `entities_found > entities_processed` | Some entities below `score_threshold` were detected but not redacted | Expected — `entities_processed` only counts ones that crossed the threshold |
| `analyze_batch` no faster than serial | `batch_size=1` or PyTorch holding the GIL | Increase batch_size; check that you're not using `device=cuda` on a single-GPU host (concurrent CUDA calls serialize) |

---

## Appendix — Related

- **toxicity/README.md** — sibling package for prompt injection / moderation classification.
- **signal-workers/README.md → Lenses → Safety** — how Signal uses both packages.
- Presidio docs: <https://microsoft.github.io/presidio/>