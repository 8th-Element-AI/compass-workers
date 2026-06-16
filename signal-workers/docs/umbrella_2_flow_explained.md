# Umbrella 2 — Retrieval Quality Flow (Explained)

> Companion to `umbrella_2_flow.png`. A demo-friendly walkthrough of how the
> Quality lens grades the **retrieval** step of a RAG system, grounded in the
> actual code (`signal_worker/lenses/quality.py` and `signal_worker/scorers.py`).
> See also the design doc `umbrella_2.md`.

## The one-sentence pitch

> "Umbrella 1 graded the *answer*. Umbrella 2 grades the *retrieval that fed the
> answer* — were the documents we pulled actually about the question, and did
> the answer even use them? It's the first metric that needs two log entries
> stitched together, not just one."

## The big picture (top to bottom)

Same assembly-line shape as umbrella 1: raw events in at the top, scoring in the
middle (yellow/orange boxes), dashboard numbers at the bottom. The one new
ingredient is the **green box** near the top — the cross-span step that makes
retrieval scoring possible.

---

## What umbrella 2 measures

It answers two questions about a RAG (retrieval-augmented generation) step:

| Metric | Plain-English question | What it catches | Thresholdable |
|---|---|---|---|
| **`context_relevance`** | "Were the documents we pulled actually about the question?" | Bad retrieval — wrong index, weak embeddings, a badly-formed search query. | ✅ |
| **`chunk_utilization`** | "Of the documents we pulled, how many did the answer actually use?" | Over-retrieval — grabbing 20 docs and using 2, wasting the context window and money. | — |

> Demo framing: relevance is about *retrieval precision* ("did we fetch the
> right stuff?"), utilization is about *retrieval efficiency* ("did we fetch too
> much stuff?"). Together they tell you whether your RAG pipeline is healthy.

Two related metrics — `context_recall` and `context_precision` — are
**deferred**: they need ground-truth relevance labels (an eval dataset) that
doesn't exist yet.

---

## The one genuinely new thing: cross-span scoring

This is the headline of umbrella 2 and the best thing to demo, because it's the
first metric that **can't be computed from a single log entry**.

A RAG interaction produces *two* separate spans that share a `trace_id`:

1. A **`retrieval` span** — the query and the chunks that came back.
2. A **`model_call` span** — the actual answer the LLM generated using those
   chunks.

To compute `chunk_utilization`, you need both: the chunks (from the retrieval
span) **and** the answer (from the model_call span). So before scoring, the
worker builds a **trace map**:

> `trace_id → all the model_call outputs in this batch`

(Code: `quality.py` lines 326–336, `_score_batch`.) That's the green box in the
diagram. When it scores a retrieval span, it looks up "what answer did this trace
eventually produce?" and feeds that in.

**The honest caveat:** this is **best-effort within a batch**. If the retrieval
span and its answer span land in *different* batches, the worker can't pair them
— so `chunk_utilization` just isn't emitted for that one (relevance still is).
The design doc flags a "trace-completion buffer" as the robust fix if real traces
start straddling batches.

---

## Walking through the boxes

**1. `signal_raw_spans` (blue cylinder, top)**
The immutable ClickHouse source. The fetch now pulls **both** `retrieval` and
`model_call` span types (the model_call rows are needed for the cross-span
answer lookup).

**2. `fetch_batch` (gray arrow)**
Same watermark/bookmark mechanism as umbrella 1 — only grabs spans newer than the
last processed point, in batches.

**3. The green box — build the trace_answers map (`quality.py`)**
The cross-span step described above: `trace_id → concat(model_call outputs)`,
built once per batch before any scoring happens.

**4. Per-retrieval-span job + dedupe + cache (`quality.py`)**
For each retrieval span, assemble a job: `(query, chunk_texts, same-trace
answer)`. The job key is `r:hash(query):hash(chunks):hash(answer)` — note the
**answer is part of the key**, so a cached score is only reused when the query,
the chunks, *and* the answer all match. Then the LRU cache check:
  - **Hit** → reuse the saved score (the line down the left side).
  - **Miss** → send the unique jobs to the scorer.

**5. The orange box — `scorers.py` (`score_retrieval`)**
One **batched call per batch** runs the two local models. Details in the deep
dive below.

**6. `scores + per-chunk evidence → content-hash LRU cache`**
Results (including per-chunk relevance scores) are cached for reuse.

**7. The yellow box — `spec.py` (turning scores into metric rows)**
`build_context` looks up the cached scores for this span's job, then **2
MetricSpecs emit** the rows: `context_relevance` and `chunk_utilization`, each
with **evidence** attached (`chunks` count, per-chunk `rel` scores, `used`
count) so the number is auditable.

**8. `signal_derived_metrics` (blue cylinder)**
One row per metric per span (EAV layout), written as one bulk insert per batch,
then the watermark advances.

**9. `mv_agg_base` → `signal_aggregated_metrics`**
A materialized view auto-fires on insert and rolls everything into 1-minute
buckets.

**10. `avgMerge` → dashboards / KPIs / signals**
Buckets get merged at read time; Postgres thresholds decide when a metric is bad
enough to fire an alert.

---

## Deep dive: the ML scoring (`scorers.py · score_retrieval`, lines 239–273)

Same prep as umbrella 1: the answer is JSON-flattened (`normalize_output`) and
split into sentences first.

### `context_relevance` — "were the chunks about the query?"

Uses the **third model**, `cross-encoder/ms-marco-MiniLM-L-6-v2`. This is a
*relevance* cross-encoder — literally trained on the task "how relevant is this
passage to this search query?" (MS-MARCO is a search-ranking dataset).

1. Pair the query with **each retrieved chunk**: `(query, chunk_text)`.
2. The model scores each pair; a **sigmoid** squashes the logit to **0–1**.
3. **`context_relevance` = the mean across all chunks.**
4. **Evidence saved:** the per-chunk scores (`rel`) so you can see *which* chunk
   was the dud.

### `chunk_utilization` — "did the answer actually use the chunks?"

Uses the **embedding model** (`all-MiniLM-L6-v2`), not the relevance one, because
it compares chunks against the *answer*, not the query.

1. Embed every chunk and every answer sentence into L2-normalized vectors.
2. A chunk counts as **"used"** if its best cosine similarity against any answer
   sentence clears **0.5** (`CHUNK_USED_COS`, `scorers.py:43`).
3. **`chunk_utilization` = used chunks / total chunks.**
4. **Evidence saved:** `used` (the count).
5. If there's no same-trace answer available in the batch, no utilization row is
   emitted.

### The models (shared with umbrella 1)

| Role | Model | Used by |
|---|---|---|
| **Relevance** | `cross-encoder/ms-marco-MiniLM-L-6-v2` | `context_relevance` |
| **Embedding** | `sentence-transformers/all-MiniLM-L6-v2` | `chunk_utilization` |

Both are small, CPU-friendly, lazy-loaded, and batched — the same instances used
by umbrella 1's scoring.

---

## Caveats worth saying out loud in a demo (all documented)

1. **Mean relevance is harsh on mixed result sets** — one great chunk among seven
   duds scores low. The doc notes "fraction of chunks above a bar" as an
   alternative aggregation.
2. **The 0.5 used-threshold is uncalibrated** — a reasonable default, not a tuned
   value. Should be set from real data; lexical n-gram overlap is a cheaper
   alternative worth A/B-ing.
3. **Trace split across batches → no utilization row** (best-effort by design).
4. **The retriever's own `score` field is ignored** (it's self-reported); it
   could be a free secondary signal.

> Great honesty-builder for the demo: on the synthetic test dataset, **all 709
> retrieval spans scored ~0 on both metrics** — and that's *the metric working
> correctly*, because the fake chunks really were unrelated to the queries. A
> real corpus produces a real distribution, which is why thresholds must be set
> from observed baselines.

---

## One-glance summary table

| Metric | Model used | Input fed in | What we read out | Final score |
|---|---|---|---|---|
| **context_relevance** | Relevance cross-encoder (MS-MARCO) | (query, each chunk) | sigmoid relevance 0–1 | mean across chunks |
| **chunk_utilization** | Embedding (MiniLM) | chunks & answer sentences as vectors | best cosine ≥ 0.5 = "used" | used / total chunks |

---

## The 3 themes to hit in your demo

1. **What it measures:** retrieval precision (`context_relevance`) and retrieval
   efficiency (`chunk_utilization`) of a RAG pipeline — does it fetch the right
   docs, and does it fetch too many?
2. **The new capability:** cross-span scoring — stitching a retrieval span to its
   sibling answer span via `trace_id` to grade something no single log entry
   could.
3. **Why it's trustworthy:** per-chunk evidence is attached to every score, and
   the ~0 scores on synthetic data prove the metric discriminates rather than
   rubber-stamps.
