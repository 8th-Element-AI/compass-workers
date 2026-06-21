# PII Detection & Severity Evaluation

Presidio-backed PII / PHI detection with quasi-identifier risk scoring, for
the Signal platform. Used by the Safety lens worker for the `pii_count` and
`pii_detected` metrics; also usable standalone as a CLI, library, or REST API.

```
text ──► PresidioEngine.analyze()   ──► AnalysisResult
                                         (risk-filtered counts + severity)
     ──► PresidioEngine.evaluate()  ──► EvaluationResult
                                         (severity-scored violations only)
     ──► PresidioEngine.detect()    ──► list[RecognizerResult]
                                         (raw span-level matches, for tooling)
```

**This package detects and risk-scores PII. It does NOT anonymize text** —
no redaction, masking, or replacement. If you need anonymization, use
Presidio's `AnonymizerEngine` directly with the spans `engine.detect()`
returns.

**Three detection modes**, picked at construction time. Same API; different
recall/latency tradeoffs.

| Mode | Detector | Latency / doc (CPU) | Recall (PERSON/LOC/DATE) |
|---|---|---|---|
| Regex-only | Presidio + custom pattern recognizers | <15 ms | low (catches structured PII: emails, SSNs, cards) |
| spaCy | `en_core_web_sm` / `lg` + Presidio | ~50 ms | medium |
| HuggingFace NER | e.g. `gravitee-io/bert-small-pii-detection` | ~330 ms | high |

Models must be downloaded **before** the engine starts. No network calls
happen during text processing.

---

## Table of contents

1. [What's in this repo](#1-whats-in-this-repo)
2. [Installation](#2-installation)
3. [Quick start (Python)](#3-quick-start-python)
4. [Entity types & policy](#4-entity-types--policy)
5. [Severity evaluation](#5-severity-evaluation)
6. [Command-line interface](#6-command-line-interface)
7. [REST API](#7-rest-api)
8. [Batch processing](#8-batch-processing)
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
├── README.md                       (this file)
├── deidentifier/
│   ├── __init__.py                 public exports
│   ├── __main__.py                 python -m deidentifier
│   ├── cli.py                      CLI entrypoint (`deidentify` script)
│   ├── api.py                      FastAPI server
│   ├── config.py                   PolicyConfig (YAML-loadable)
│   ├── entities.py                 EntityType enum (22 supported types)
│   ├── result.py                   AnalysisResult, EvaluationResult
│   ├── policy_evaluator.py         Severity, ComboRule, COMBO_RULES,
│   │                               GranularityScorer, SentenceGrouper,
│   │                               PolicyEvaluator
│   ├── policies/
│   │   └── default.yaml            Built-in default policy
│   └── presidio/
│       ├── engine.py               PresidioEngine — the singleton +
│       │                           analyze/detect/evaluate API
│       └── recognizers.py          Custom Presidio PatternRecognizers
│                                   (dates, MRN, age, ZIP, NPI, bank,
│                                    MBI, URL, ORG, gender, SSN)
└── eval/
    ├── evaluate.py                 IOU-based eval against gold dataset
    ├── benchmark_models.py         Compare 13 different NER models
    └── BENCHMARK_RESULTS.md        Recent benchmark numbers
```

Public exports from `deidentifier`:

```python
from deidentifier import (
    PresidioEngine,
    AnalysisResult, EvaluationResult,
    PolicyConfig, EntityType,
    PolicyEvaluator, Severity, Violation,
)
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

The engine refuses to download at runtime by design — no surprise network
calls in production pods. If the model isn't in the HF cache when
`PresidioEngine` starts, it raises `RuntimeError` with a hint pointing at
the command above.

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
```

### `analyze()` — risk-aware detection (the common path)

```python
result = engine.analyze("A man aged 42 joined the hospital on 20 May 2024.")

# Risk-aware fields — what the Safety worker reads
result.has_pii         # True
result.severity        # Severity.PHI
result.entity_count    # 3  (only entities contributing to a violation)
result.entities        # {'AGE': 1, 'GENDER': 1, 'DATE_TIME': 1}

# Severity layer
result.has_violation
result.violations      # [Violation(rule_name='age_gender_date_medical', …)]

# Raw detection layer (always populated, for debugging)
result.raw_entity_count
result.raw_entities    # everything detected, pre-filter
```

For a sentence with detectable components but no re-identification risk:

```python
result = engine.analyze("A man aged 42 years")
result.has_pii        # False — no combo rule fires
result.entity_count   # 0
result.severity       # Severity.NONE
result.raw_entities   # {'AGE': 1, 'GENDER': 1}   (still visible for debugging)
```

### `evaluate()` — severity-only

When you just want violations, not counts:

```python
ev = engine.evaluate("John Smith, DOB 12/03/1985, lives in 94103")
ev.has_violation   # True
ev.max_severity    # Severity.HIGH
ev.violations      # [Violation(rule_name='name_dob_location', …)]
```

### `detect()` — span-level

When you need positions for downstream tooling (e.g., anonymization,
highlighting):

```python
matches = engine.detect("Email me at jane@example.com")
for m in matches:
    print(m.entity_type, m.start, m.end, m.score)
# EMAIL_ADDRESS 12 28 0.95
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

## 4. Entity types & policy

### Supported entity types (22)

```
CREDIT_CARD, DATE_TIME, EMAIL_ADDRESS, IBAN_CODE, IP_ADDRESS, LOCATION,
MEDICAL_LICENSE, PERSON, PHONE_NUMBER, URL, NRP, US_BANK_NUMBER,
US_DRIVER_LICENSE, US_PASSPORT, US_SSN, DATE_OF_BIRTH, MEDICAL_RECORD_NUMBER,
AGE, ZIP_CODE, MEDICARE_ID, ORG, GENDER
```

The `EntityType` enum in `entities.py` is the canonical list.

### Date format coverage

The custom date recognizers (`presidio/recognizers.py`) catch formats that
Presidio's built-ins miss:

| Format | Detected |
|---|---|
| `20/05/2024`, `2024-05-20`, `05/20/24` | ✅ Presidio built-in |
| `20 May 2024`, `May 20, 2024` | ✅ Presidio built-in / HF NER |
| `20thmay2024`, `1stJan2025` | ✅ custom (compact + ordinal) |
| `20may2024`, `5dec99` | ✅ custom (compact, no ordinal) |
| `20-May-2024`, `20.May.2024`, `20/May/2024` | ✅ custom (separator + month name) |
| `20240520` (ISO compact) | ✅ custom (with strict month/day validation) |

### GENDER

Detects explicit gender nouns (`man`, `woman`, `male`, `female`, `boy`, `girl`,
`gentleman`, `lady`, plural forms), honorifics (`Mr.`, `Mrs.`, `Ms.`, `Mx.`,
`Mister`), and the clinical key-value form (`Gender: Male`, `Sex: F`).

**Pronouns are intentionally excluded** (`he`, `she`, `his`, `her`, `him`,
`hers`) — they appear in nearly every paragraph and would explode false
positives. Gender alone is scored low; it's only meaningful in combination
with other quasi-identifiers (see Sweeney's k-anonymity work).

### Policy YAML

`PolicyConfig` controls two things: the minimum confidence score for
detections, and which entity types are enabled. The default policy lives at
`deidentifier/policies/default.yaml`:

```yaml
version: "1.0"
score_threshold: 0.7
entities:
  PERSON:
    enabled: true
  EMAIL_ADDRESS:
    enabled: true
  US_SSN:
    enabled: true
  GENDER:
    enabled: true
  # ... all 22 entities, all enabled by default
```

Note: when constructing `PresidioEngine(ner_model=<HF model>)` without an
explicit policy, the engine overrides `score_threshold` to **0.35** —
calibrated for HuggingFace NER models which produce lower scores than
Presidio's hand-tuned regex recognizers.

Use a custom policy:

```python
from deidentifier.config import PolicyConfig
policy = PolicyConfig.from_yaml("./my-policy.yaml")
engine = PresidioEngine(policy=policy)
```

Or via CLI: `deidentify input.txt --policy ./my-policy.yaml`.

---

## 5. Severity evaluation

This is the layer that turns raw detections into risk decisions. Two things
matter for PII/PHI risk: (a) whether the components found are uniquely
identifying, and (b) whether they appear near medical context.

`PolicyEvaluator` runs three tiers per sentence-grouped cluster of entities:

| Tier | What it checks | Output severity |
|---|---|---|
| **Tier 0** | Any single direct identifier (SSN, email, MRN, …) | `HIGH` (or `PHI` near medical) |
| **Tier 1** | Named entity-type combinations (Sweeney-style k-anonymity) | per rule (or `PHI` near medical) |
| **Tier 2** | Cumulative quasi-identifier granularity score backstop | `MEDIUM` (or `PHI` near medical) |

### Direct identifiers

These fire Tier 0 by themselves — no combination needed:

```
EMAIL_ADDRESS, PHONE_NUMBER, US_SSN, CREDIT_CARD, US_DRIVER_LICENSE,
US_PASSPORT, IP_ADDRESS, IBAN_CODE, MEDICAL_LICENSE,
MEDICAL_RECORD_NUMBER, US_BANK_NUMBER, MEDICARE_ID, URL
```

PERSON is intentionally **not** a direct identifier — bare names aren't
uniquely identifying (millions of Sarahs exist). It's scored as a
quasi-identifier instead, via `GranularityScorer` (first-name-only vs full
name).

### Combination rules

Tier 1 has 16 combination rules, evaluated **most-specific-first**. Each
requires specific entity types co-occurring in a sentence; some require
minimum granularity per type or medical context to fire.

| Rule | Entities | Base severity | Notes |
|---|---|---|---|
| `age_gender_date_medical` | AGE + GENDER + DATE_TIME | HIGH | requires medical context → PHI |
| `sweeney_canonical` | DATE_OF_BIRTH + ZIP_CODE + GENDER | HIGH | Sweeney's 87% triad |
| `sweeney_location_gender` | DATE_OF_BIRTH + LOCATION + GENDER | HIGH | LOCATION ≥ city-level |
| `sweeney_triad` | DATE_OF_BIRTH + ZIP_CODE + NRP | HIGH | legacy variant |
| `name_dob_location` | PERSON + DATE_OF_BIRTH + LOCATION | HIGH | |
| `name_date_location` | PERSON + DATE_TIME + LOCATION | HIGH | |
| `person_org_location` | PERSON + ORG + LOCATION | HIGH | |
| `person_age_location` | PERSON + AGE + LOCATION | MEDIUM | |
| `age_gender_zip` | AGE + GENDER + ZIP_CODE | MEDIUM | Sweeney-light |
| `age_gender_location` | AGE + GENDER + LOCATION | MEDIUM | |
| `age_zip_org` | AGE + ZIP_CODE + ORG | MEDIUM | |
| `date_org_location` | DATE_TIME + ORG + LOCATION | MEDIUM | |
| `name_dob` | PERSON + DATE_OF_BIRTH | HIGH | |
| `name_zip` | PERSON + ZIP_CODE | HIGH | full name only |
| `name_org` | PERSON + ORG | MEDIUM | full name only |
| `age_date_medical` | AGE + DATE_TIME | MEDIUM | requires medical context → PHI |

Rules are processed in order; once an entity is matched by a rule, it's
marked "covered" and won't contribute to subsequent rules. This is why
ordering matters — a 3-entity rule must come before any 2-entity rule that
uses the same types, otherwise the 2-entity rule wins first.

### Medical context

The evaluator scans a 200-character window around each entity group for
clinical keywords (`patient`, `diagnos*`, `hospital*`, `medication*`, drug
names, conditions, ICD/CPT codes, etc.). When detected, any combination-rule
violation in that group is bumped from its base severity to `PHI`. This is
the mechanism that turns "John, age 42" into PHI when it appears in a
hospital admission record context.

Two rules (`age_gender_date_medical`, `age_date_medical`) require medical
context to fire at all — they're meaningless without it. Every meeting
schedule would otherwise fire them.

### What `analyze()` does with this

`analyze()` runs detection, then severity evaluation, then filters: only
entities contributing to a violation of severity `>= MEDIUM` are counted in
`entity_count` / `entities` / `has_pii`. The raw detection signal is
preserved in `raw_entity_count` / `raw_entities` for debugging.

Worked examples:

| Input | Detected (raw) | Violations | `has_pii` | severity |
|---|---|---|---|---|
| `"A man aged 42 years"` | AGE, GENDER | none | False | NONE |
| `"Email me at jane@x.com"` | EMAIL_ADDRESS | Tier 0 (HIGH) | True | HIGH |
| `"Patient aged 42 admitted on 20 May 2024"` | AGE, DATE_TIME | `age_date_medical` → PHI | True | PHI |
| `"A man aged 42 years joined a hospital on 20thmay2024"` | AGE, GENDER, DATE_TIME | `age_gender_date_medical` → PHI | True | PHI |
| `"John Smith, DOB 12/03/1985, lives in 94103"` | PERSON, DOB, ZIP_CODE | `name_dob` → HIGH | True | HIGH |
| `"John Smith works at Google"` | PERSON, ORG | `name_org` → MEDIUM | True | MEDIUM |

---

## 6. Command-line interface

After install you get the `deidentify` script (and `python -m deidentifier`):

```bash
# Regex-only (no ML, instant startup)
deidentify notes.txt

# With HuggingFace NER
deidentify notes.txt --ner-model gravitee-io/bert-small-pii-detection

# Write report to file
deidentify notes.txt --output report.txt

# Lower the score threshold
deidentify notes.txt --score-threshold 0.35

# JSON output (includes per-entity details)
deidentify notes.txt --format json

# Custom policy
deidentify notes.txt --policy ./my-policy.yaml

# Also run severity evaluation (adds violations + max_severity)
deidentify notes.txt --evaluate --format json
```

`--help` prints the full option list.

---

## 7. REST API

`deidentifier/api.py` is a FastAPI server suitable for sidecar deployment.

```bash
pip install -e ".[api]"   # or just: pip install fastapi uvicorn
uvicorn deidentifier.api:app --host 0.0.0.0 --port 8000
```

Endpoints:

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/health` | — | `{status, ner_model, ready}` |
| POST | `/analyze` | `{text, document_id?}` | `AnalyzeResponse` (counts + severity-filtered) |
| POST | `/analyze/batch` | `{texts, document_ids?}` | `BatchResponse` |
| POST | `/analyze/plain` | raw text body (text/plain) | `AnalyzeResponse` |
| POST | `/evaluate` | `{text, document_id?}` | `EvaluateResponse` (violations + max_severity) |

Configure the NER model via env:

```bash
DEIDENTIFIER_NER_MODEL=gravitee-io/bert-small-pii-detection
```

Empty string → regex-only mode. The server uses the singleton, so model load
happens once on startup.

---

## 8. Batch processing

For >5 texts at once, **always use `analyze_batch`** — it parallelizes
across texts with a `ThreadPoolExecutor` and gives you ~`batch_size`× speedup
on a multi-core CPU. The sequential method is kept for callers that don't
want concurrency:

| Method | Concurrency | Returns | Recommended |
|---|---|---|---|
| `engine.analyze_batch(texts, batch_size=4)` | parallel (threadpool) | `list[AnalysisResult]` | ✅ |
| `engine.batch_analyze(texts)` | sequential | `list[AnalysisResult]` | only if concurrency unsafe in caller |

Sane defaults: `batch_size=4` on a 4-core box, scale up linearly with cores
up to ~16. Above that, Presidio's per-text overhead dominates and concurrency
plateaus.

```python
texts = [open(f).read() for f in glob("notes/*.txt")]
results = engine.analyze_batch(texts, batch_size=8)

flagged = [(i, r) for i, r in enumerate(results) if r.has_pii]
```

Identical texts within a batch are deduplicated — analyzed once, their
result reused for every duplicate position.

---

## 9. Models — what to pick and why

| Goal | Recommended `ner_model` | Latency | Recall | Notes |
|---|---|---|---|---|
| Compliance scans on structured docs (cards, IDs, emails) | `None` | <15 ms | low for free-text PII | Regex-only is enough; the patterns catch all structured PII formats |
| General observability, no name catch needed | `en_core_web_sm` | ~50 ms | medium | spaCy small. Cheap. |
| Production observability with name/location catch | `gravitee-io/bert-small-pii-detection` | ~330 ms | high | What the Signal Safety worker uses |
| Medical (clinical notes) | `obi/deid_roberta_i2b2` | ~600 ms | very high (medical) | Specifically trained on i2b2 |
| Highest recall, any domain | `iiiorg/piiranha-v1-detect-personal-information` | ~500 ms | very high | Largest of the recommended models |

See `eval/BENCHMARK_RESULTS.md` for F1 scores on a 500-doc evaluation set
across all 13 benchmarked models.

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

`analyze()` adds ~1–2 ms over raw detection for the severity-evaluation pass
(sentence grouping, granularity scoring, combo iteration — pure Python on a
handful of entities).

### Throughput with `analyze_batch(batch_size=N)`

Linear-ish up to physical core count, then plateaus.

| batch_size | docs/sec (HF NER, 4-core machine) |
|---|---|
| 1 | 3 |
| 4 | 11 |
| 8 | 18 |
| 16 | 21 |

The Safety worker uses `batch_size=4` by default (`SIGNAL_PII_BATCH_SIZE=4`).

### Running the benchmark yourself

```bash
pip install kaggle
kaggle datasets download alejopaullier/pii-external-dataset -p eval/data --unzip

# Single model
python eval/evaluate.py --max-docs 500

# All 13 models, side by side
python eval/benchmark_models.py --max-docs 500
```

---

## 11. Use inside Signal Workers

The Safety lens worker imports this package as `deidentifier`:

```python
from deidentifier import PresidioEngine

engine = PresidioEngine.get_instance(ner_model=os.environ["SIGNAL_PII_NER_MODEL"])
results = engine.analyze_batch(unique_texts,
                               batch_size=int(os.environ["SIGNAL_PII_BATCH_SIZE"]))
```

Each `AnalysisResult` carries:
- `entity_count` / `entities` / `has_pii` — risk-filtered (used by the worker)
- `severity` / `violations` — available for richer metric_meta if you want them
- `raw_entity_count` / `raw_entities` — raw detections for debugging

The worker emits:
- `pii_count` ← `result.entity_count` (risk-aware count)
- `pii_detected` ← `pii_count > 0` (real risk, not raw component matches)
- `pii_types` ← keys of `result.entities`

Relevant Signal env vars:

| Env var | Default | What it does |
|---|---|---|
| `SIGNAL_PII_NER_MODEL` | `gravitee-io/bert-small-pii-detection` | Picks the NER model |
| `SIGNAL_PII_BATCH_SIZE` | `4` | ThreadPool width for `analyze_batch` |
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
| Engine returns `entity_count=0` but PII is clearly present | `score_threshold` too high in policy | Lower it (e.g. `0.35` for HF NER models, automatically applied when no policy is passed) |
| Detection results have low scores on names | Using regex-only mode | Switch to spaCy or HF NER |
| `has_pii=False` but `raw_entity_count>0` | Detections happened but no violation of severity ≥ MEDIUM | Expected — components alone (e.g. bare AGE, GENDER) don't fire `has_pii`. Inspect `result.violations` and `result.raw_entities` |
| `has_pii=True` from a sentence that "looks safe" | A combination rule matched | Inspect `result.violations[0].rule_name` and tune `min_granularity` or `requires_medical_context` on that rule |
| First call hangs 5–10s | First-time model load (spaCy or HF) | Use the singleton (`get_instance()`) and warm it during startup |
| `analyze_batch` no faster than serial | `batch_size=1` or PyTorch holding the GIL | Increase batch_size; check that you're not using `device=cuda` on a single-GPU host (concurrent CUDA calls serialize) |

---

## Appendix — Related

- **toxicity/README.md** — sibling package for prompt injection / moderation classification.
- **signal-workers/README.md → Lenses → Safety** — how Signal uses both packages.
- Presidio docs: <https://microsoft.github.io/presidio/>
- Sweeney, L. (2000). *Simple Demographics Often Identify People Uniquely.* — origin of the {DOB, ZIP, gender} canonical triad.