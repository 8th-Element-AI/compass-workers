# Umbrella 1 — Output Scoring (Quality Lens)

> Design doc for the first of four Quality-lens metric umbrellas.
> Status: v1 implemented (local-model scoring) · Owner: Quality lens worker

---

## 1. Metrics captured

All three are **semantic** metrics on `model_call` spans — they require something to
*read the text and judge*; none are computable from span columns or metadata lookups.

| Metric | Type | Range | Question it answers | Failure it catches | Thresholdable |
|---|---|---|---|---|---|
| `faithfulness` | float | 0–1 | Is every claim in the output supported by the provided context? | Hallucination | ✅ |
| `coherence` | float | 0–1 | Is the output internally consistent? | Contradictions, broken reasoning | ✅ |
| `completeness` | float | 0–1 | Does the output cover what the task required? | Partial answers, dropped aspects | ✅ |

**Data contract** — a `model_call` span must carry:

| Field | Used for | Note |
|---|---|---|
| `metadata.input` | faithfulness (grounding premise), completeness | must include the grounding context |
| `metadata.output` | all three | the generated text |

Emit-when-present: a span missing either field produces **no row** — never a fabricated score.

---

## 2. Where this sits in signal-workers

```
signal-workers/
├── run_worker.py                      registers "quality" in LENSES
└── signal_worker/
    ├── config.py                      SIGNAL_QUALITY_* knobs (sampling, models, cache)
    ├── base.py                        run loop · watermark · ClickHouse I/O   (untouched)
    ├── spec.py                        MetricSpec engine + meta_fn audit hook  (untouched)
    ├── patterns.py                    ctx_value() — how specs read scores     (untouched)
    ├── predicates.py                  llm_call — which spans this umbrella applies to
    ├── scorers.py                 ⭐  THE METRIC-CAPTURING COMPONENT (see §4)
    └── lenses/
        └── quality.py             ⭐  batching, caching, sampling + the 3 specs (see §5)
```

Everything below the lens is **unchanged shared framework**: the same watermark,
batch fetch, derived-table insert, and materialized-view rollup that every other
lens uses. This umbrella adds exactly two components — the scorer and the lens.

---

## 3. End-to-end data flow

```
signal_raw_spans (ClickHouse)
   │  fetch_batch — WHERE span_type IN (...) pushed into the query (base.py)
   ▼
QualityWorker.process_batch                                  [lenses/quality.py]
   │
   ├─ 1. collect (input, output) pairs from SAMPLED model_call spans
   ├─ 2. dedupe by content hash · skip pairs already in the LRU cache
   ├─ 3. ONE batched call:  scorer.score_generation(unique_pairs)   ──►  scorers.py ⭐
   ├─ 4. results → content-hash LRU cache (SIGNAL_QUALITY_CACHE_MAX)
   ▼
SpecWorker.compute (per span)                                [spec.py — engine]
   │   build_context reads the cache → ctx{faithfulness, coherence, completeness}
   │   specs emit one row per metric via ctx_value()
   │   evidence (sentence counts, min entailment) → metric_meta column
   ▼
signal_derived_metrics  ──(mv_agg_base)──►  signal_aggregated_metrics
                                             └─► dashboards · thresholds · signals
```

Why this shape (precedent: the Safety lens / Presidio):

- **Batched inference** — model calls are the cost; one call covers the whole batch.
- **Content-hash dedupe + LRU cache** — identical prompts are judged once
  (~2.3× dedupe observed on sample data).
- **Deterministic sampling** — `SIGNAL_QUALITY_SAMPLE` hashes `span_id`, so reruns
  score the same subset (stable, auditable coverage).
- **Kill switch** — `SIGNAL_QUALITY_SEMANTIC=false` disables this umbrella entirely;
  the lens still emits its mechanical metrics, and torch is never imported.

---

## 4. ⭐ Metric-capturing component #1: `signal_worker/scorers.py`

The judge. Defined as a **pluggable interface** so the backend can be swapped
without touching any spec (same seam pattern as `PricingSource` in `pricing.py`).

```
QualityScorer (interface)
 ├── LocalScorer    ◄── v1 production: three small local models, lazy-loaded
 └── StaticScorer   ◄── tests / offline smoke runs (fixed scores, no torch)
```

### Models (LocalScorer, all CPU/MPS-friendly, lazy-loaded on first batch)

| Model | Role | Config knob |
|---|---|---|
| `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | entailment / contradiction (FEVER-trained, benchmarked best) | `SIGNAL_QUALITY_NLI_MODEL` |
| `sentence-transformers/all-MiniLM-L6-v2` | sentence embeddings | `SIGNAL_QUALITY_EMBED_MODEL` |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | (used by Umbrella 2) | `SIGNAL_QUALITY_RELEVANCE_MODEL` |

### Capture recipe per metric

| Metric | Recipe | Evidence in `metric_meta` |
|---|---|---|
| `faithfulness` | split output into sentences (JSON flattened to `"key is value."` lines) → NLI entailment of each sentence vs the input (premise, 2k-char cap) → **mean entailment prob** | `out_sents`, `min_entail` (the least-supported sentence = the hallucination pointer) |
| `coherence` | NLI **contradiction** prob over adjacent output sentence pairs → `1 − mean`; needs ≥2 sentences, else no row | `out_sents` |
| `completeness` | embed input + output sentences → mean over input sentences of best cosine match in output (**input-coverage proxy**) | — |

Controlled-example sanity check (verified):
grounded output → faithfulness **0.98** · hallucinated → **0.0001** ·
contradictory text → coherence **0.18** · coherent → **0.999**.

### Known proxy limitations (by design, documented)

1. **Extraction-style JSON outputs score low on faithfulness** (~0.22 mean on sample
   data) — `"doc_type is invoice"` is not linguistically *entailed* by an invoice.
   Planned fix: deterministic value-grounding check for structured outputs.
2. **Completeness measures input coverage**, not "required aspects" — penalizes
   legitimate summarization/extraction. Candidate redefinition: derive from declared
   requirements / Umbrella-4 schema for structured outputs.
3. **Coherence is adjacent-pair only** — blind to long-range contradictions.

### The upgrade ladder (why the interface matters)

| Rung | Judge | Cost | When |
|---|---|---|---|
| 1 | **LocalScorer (current)** | hardware only | now |
| 2 | LLM judge (rubric prompt, same interface) | per-token | when scores drive alerts/gating |
| 3 | Hybrid: local scores triage, LLM judges the suspicious tail | bounded | likely end-state |

Moving up a rung = new `QualityScorer` implementation + config. Zero spec changes.

---

## 5. ⭐ Metric-capturing component #2: `signal_worker/lenses/quality.py`

The lens owns *when and for which spans* scoring happens; the scorer owns *how*.

| Piece | Responsibility |
|---|---|
| `SPECS` (3 entries for this umbrella) | declarative registry: metric name, `llm_call` predicate, `ctx_value()` read, unit/window/threshold, `meta_fn` audit hook |
| `process_batch()` override | sampling → dedupe → cache check → one scorer call → cache fill |
| `build_context()` | per span: look up cached scores, expose as ctx fields |
| LRU cache + `_sampled()` | content-hash keyed; deterministic span_id sampling |

Spec declarations (the umbrella's full footprint in the registry):

```python
_spec("faithfulness", llm_call, ctx_value("faithfulness"), ..., threshold=True, meta_fn=_gen_meta)
_spec("coherence",    llm_call, ctx_value("coherence"),    ..., threshold=True)
_spec("completeness", llm_call, ctx_value("completeness"), ..., threshold=True)
```

---

## 6. Configuration

| Env var | Default | Purpose |
|---|---|---|
| `SIGNAL_QUALITY_SEMANTIC` | `true` | umbrella on/off (off = no torch import) |
| `SIGNAL_QUALITY_SAMPLE` | `1.0` | fraction of spans scored, deterministic by span_id |
| `SIGNAL_QUALITY_NLI_MODEL` | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | faithfulness/coherence |
| `SIGNAL_QUALITY_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | completeness |
| `SIGNAL_QUALITY_BATCH` | `32` | model inference batch size |
| `SIGNAL_QUALITY_CACHE_MAX` | `20000` | LRU cap on the scoring cache |

Dependency: `sentence-transformers` (pulls torch). Only imported when semantic
scoring actually runs.

---

## 7. Downstream (nothing umbrella-specific)

- One row per metric per scored span → `signal_derived_metrics` (EAV, append-only).
- `mv_agg_base` rolls into 1-minute buckets automatically; dashboards read
  `avgMerge` / `quantilesTDigestMerge` like any other metric.
- All three are `threshold=True` → Postgres thresholds can alert
  (e.g. *p10 faithfulness < 0.3 over 1h*).
- ⚠️ Thresholds should be set from **observed per-solution baselines**, not absolute
  intuition — proxy scores are relative signals (0.6 faithfulness ≠ "60% true").

## 8. Open decisions

1. Value-grounding faithfulness variant for structured/JSON outputs (recommended).
2. Completeness redefinition: schema/requirements-driven for structured outputs.
3. Production sampling default once real volume is known (v1: 100%; ~3k spans
   score in minutes on a laptop).
4. Agreed trigger for the rung-2 LLM-judge upgrade.
5. Instrumentation guideline: grounding context must live in `metadata.input`
   (else faithfulness needs trace-join, like `chunk_utilization`).
