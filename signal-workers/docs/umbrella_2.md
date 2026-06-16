# Umbrella 2 — Retrieval Quality (Quality Lens)

> Design doc for the second of four Quality-lens metric umbrellas.
> Status: v1 implemented (local-model scoring) · Owner: Quality lens worker
> Companion diagram: `umbrella_2_flow.png`

---

## 1. Metrics captured

Both metrics describe the **query → chunks → answer relationship** of a RAG step.
This is the system's first **cross-span** umbrella: `chunk_utilization` needs the
answer produced by the *sibling* `model_call` in the same trace.

| Metric | Type | Range | Question it answers | Failure it catches | Thresholdable |
|---|---|---|---|---|---|
| `context_relevance` | float | 0–1 | Are the retrieved chunks actually about the query? | Bad retrieval (wrong index, weak embedding, bad query) | ✅ |
| `chunk_utilization` | float | 0–1 | What fraction of retrieved chunks did the answer actually use? | Over-retrieval, wasted context window, top_k too high | — |

Deferred from this umbrella (catalog-tagged `eval` — need ground-truth relevance
labels that don't exist yet): `context_recall`, `context_precision`.

**Data contract** — a `retrieval` span must carry:

| Field | Used for | Note |
|---|---|---|
| `metadata.query` | context_relevance | the search query |
| `metadata.chunks[]` | both | list of `{chunk_id, text, score}` — **full chunk text required** |
| same-trace `model_call` `metadata.output` | chunk_utilization | resolved via `trace_id`, best-effort within the batch |

Emit-when-present: no chunks → no rows; no same-trace answer found → no
`chunk_utilization` row (relevance still emits).

---

## 2. Where this sits in signal-workers

Identical component footprint to Umbrella 1 — same two ⭐ components, no new
framework pieces:

```
signal_worker/
├── scorers.py                 ⭐  score_retrieval() — the capture recipes (§4)
└── lenses/quality.py          ⭐  trace-answer map + batching/caching + 2 specs (§5)
```

The cross-span lookup is the one structural novelty: `process_batch` builds a
`trace_id → model_call outputs` map for the batch **before** scoring, so a
retrieval span can see the answer its chunks fed into.

---

## 3. End-to-end data flow

```
signal_raw_spans
   │  fetch_batch (span_type filter incl. retrieval + model_call)
   ▼
QualityWorker.process_batch
   ├─ 1. build trace_answers: trace_id → concat(model_call outputs)   ◄── cross-span step
   ├─ 2. per retrieval span: job (query, chunk_texts, answer)
   │       key = r:hash(query):hash(chunks):hash(answer) · deduped · LRU-checked
   ├─ 3. ONE batched call: scorer.score_retrieval(unique_jobs)
   ▼
build_context → ctx{context_relevance, chunk_utilization} → 2 specs emit
   │     per-chunk relevance scores + used-count → metric_meta (audit)
   ▼
signal_derived_metrics ──(mv_agg_base)──► signal_aggregated_metrics ──► dashboards
```

---

## 4. ⭐ Capture recipes (`scorers.py · score_retrieval`)

| Metric | Recipe | Evidence in `metric_meta` |
|---|---|---|
| `context_relevance` | MS-MARCO relevance cross-encoder scores each `(query, chunk_text)` pair → sigmoid to 0–1 → **mean across chunks** | `chunks` (count), `rel` (per-chunk scores, rounded) |
| `chunk_utilization` | embed chunks + answer sentences (answer JSON-flattened first) → chunk counts as **used** if its best cosine vs any answer sentence ≥ **0.5** → used / total | `used` (count) |

Models: `cross-encoder/ms-marco-MiniLM-L-6-v2` (relevance — this is literally the
task it was trained on) and `all-MiniLM-L6-v2` (embeddings). Both lazy-loaded,
batched, shared with Umbrella 1.

Controlled-example sanity check (verified): relevant chunk → rel **1.0**, counted
used; irrelevant chunk → rel **0.0**, not used.

### Live-run observation (full sample dataset, real ClickHouse run)

`context_relevance ≈ 0.0002` and `chunk_utilization = 0` across all 709 retrieval
spans — **this is the metric working**: the synthetic chunks are genuinely
unrelated to the queries and answers. A real corpus will produce a real
distribution; thresholds must be set from observed baselines.

### Known limitations (by design, documented)

1. **Mean relevance punishes mixed result sets** — one great chunk among seven
   duds scores low. Alternative aggregation: "fraction of chunks above a bar."
2. **The 0.5 used-threshold is uncalibrated** — pick it from realistic data;
   lexical n-gram overlap is a cheaper alternative worth A/B-ing.
3. **Trace split across batches → no utilization row** (best-effort by design;
   the trace→answer map is per batch). A trace-completion buffer is the robust
   fix if real traces straddle batches.
4. The retriever's own `score` field is ignored (self-reported); could be a free
   secondary signal.

---

## 5. ⭐ Lens footprint (`lenses/quality.py`)

```python
_spec("context_relevance", retrieval_op, ctx_value("context_relevance"), ..., threshold=True, meta_fn=_ret_meta)
_spec("chunk_utilization", retrieval_op, ctx_value("chunk_utilization"), ...)
```

| Piece | Responsibility |
|---|---|
| `_score_batch()` | builds the trace→answer map, collects/dedupes retrieval jobs |
| `_ret_key()` / LRU cache | content-hash caching incl. the answer hash |
| `build_context()` | cache lookup per span, exposes ctx fields |

Same config knobs as Umbrella 1 (`SIGNAL_QUALITY_SEMANTIC`, `_SAMPLE`,
`_RELEVANCE_MODEL`, `_EMBED_MODEL`, `_BATCH`, `_CACHE_MAX`).

---

## 6. Open decisions

1. **Aggregation semantics** — mean vs fraction-above-bar for relevance.
2. **Used-threshold calibration** (0.5) once realistic data exists; lexical
   overlap as alternative.
3. **Trace boundary policy** — accept best-effort, or buffer incomplete traces.
4. **`context_recall` / `context_precision`** — revisit when an eval dataset
   with ground-truth relevance labels exists (judge-approximation is possible
   but was descoped).
