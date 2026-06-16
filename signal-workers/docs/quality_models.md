# Quality Lens — Metrics, Models & Alternatives

> Catalog of all 11 P0 quality metrics across the four umbrellas: **what model
> (if any) computes each metric, how it works, and which alternative models could
> replace it.** The companion evaluation plan (precision/recall/F1 of the
> model-based metrics against labeled datasets) is in §6 and follows the house
> benchmark pattern from `PII/eval/BENCHMARK_RESULTS.md`.

---

## 1. The two camps (read this first)

The 11 metrics divide into two groups with **fundamentally different evaluation
stories**:

| Camp | Metrics | Compute | Evaluable with P/R/F1? |
|---|---|---|---|
| **Model-based** (umbrellas 1–2) | faithfulness, coherence, completeness, context_relevance, chunk_utilization | ML models (NLI / embeddings / cross-encoder) → 0–1 float | ✅ — after labeling + thresholding (see §6) |
| **Mechanical** (umbrellas 3–4) | schema_conformance, format_correctness, constraint_satisfaction, tool_call_validity, data_completeness, data_accuracy | deterministic rules over span metadata | ❌ — no model; correctness via golden/unit tests |

> **Why the split matters for this doc:** "evaluate the models against an eval
> dataset and get precision/recall/F1" only makes sense for the **5 model-based
> metrics**. The 6 mechanical metrics have no model to benchmark — their
> correctness is a matter of rule coverage, validated with golden test cases, not
> a model leaderboard.

All model-based metrics are produced by the pluggable `QualityScorer`
(`signal_worker/scorers.py`); swapping any model below is an interface change with
zero spec edits.

---

## 2. Models currently in use

Three local models, all CPU/MPS-friendly, lazy-loaded, shared across metrics:

| Model | HF id | Params | Role | Metrics it powers |
|---|---|---|---|---|
| **NLI cross-encoder** | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | ~184M | entailment / contradiction over sentence pairs | faithfulness, coherence |
| **Sentence embedder** | `sentence-transformers/all-MiniLM-L6-v2` | ~22M | 384-dim sentence vectors, cosine similarity | completeness, chunk_utilization |
| **Relevance reranker** | `cross-encoder/ms-marco-MiniLM-L-6-v2` | ~22M | query↔passage relevance score | context_relevance |

> **NLI model updated after benchmarking** (see `QUALITY_BENCHMARK_RESULTS.md`):
> the original `cross-encoder/nli-deberta-v3-xsmall` (~22M) scored AUROC 0.406 on
> HaluEval faithfulness — below chance. The FEVER+ANLI model above scored 0.739.
> `LocalScorer` reads the NLI label order from the model config, so either drops
> in via `SIGNAL_QUALITY_NLI_MODEL`.

The embedder and reranker stay small and CPU-friendly — benchmarking showed their
larger alternatives buy ≤0.04 AUROC for 7–70× the latency, so the small-model
floor is well-chosen there. The faithfulness NLI model is the one place the extra
size paid off.

---

## 3. Umbrella 1 — Output scoring (model-based)

### 3.1 `faithfulness` — is the output grounded in the input context?

- **Span:** `model_call`.
- **Input:** `metadata.input` (grounding context → NLI *premise*, ≤2k chars) +
  `metadata.output` (each sentence → NLI *hypothesis*).
- **Output:** `score` — 0–1 float, **mean entailment probability** (higher =
  better grounded; `None` when input is empty).
- **Model:** `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` (FEVER+ANLI NLI) —
  **adopted after benchmarking** (AUROC 0.739 vs 0.406 for the original xsmall).
- **How:** output is split into sentences (JSON flattened to `"key is value."`
  lines); each sentence is the *hypothesis*, the input (≤2k chars) is the
  *premise*; score = **mean entailment probability** across sentences. Low
  entailment ⇒ unsupported claim ⇒ likely hallucination.
- **Evidence:** `min_entail` (least-supported sentence) saved to `metric_meta`.

| Alternative model | Size | Why consider it | Trade-off |
|---|---|---|---|
| `cross-encoder/nli-deberta-v3-xsmall` | 22M | the prior default — fastest | AUROC 0.406 (below chance) on HaluEval |
| `cross-encoder/nli-deberta-v3-base` | 184M | same family, stronger NLI head | AUROC 0.485 — generic NLI ≠ fact-verification |
| `facebook/bart-large-mnli` | 407M | classic strong zero-shot NLI baseline | large, slow on CPU |
| **`vectara/hallucination_evaluation_model` (HHEM-2.1)** | ~184M | **purpose-built for grounding/faithfulness**, not generic NLI | different output contract (single grounded-prob) |
| LLM-as-judge (RAGAS faithfulness / Claude / GPT-4o) | API | highest fidelity; handles structured outputs natively | per-call cost, latency, external dependency |

### 3.2 `coherence` — is the output internally consistent?

- **Span:** `model_call`.
- **Input:** `metadata.output` only (split into adjacent sentence pairs).
- **Output:** `score` — 0–1 float, `1 − mean(contradiction)` (higher = more
  consistent; `None` when there are <2 sentences to compare).
- **Model:** same NLI cross-encoder.
- **How:** NLI **contradiction** probability over adjacent output sentence pairs;
  score = `1 − mean(contradiction)`. Needs ≥2 sentences (else no row).
- **Alternatives:** same NLI ladder as faithfulness; plus **embedding-drift**
  (consecutive-sentence cosine via the embedder — catches topic breaks NLI
  misses) and **repetition-rate** as a cheap degenerate-loop companion. LLM-judge
  for holistic reasoning-flow assessment.

### 3.3 `completeness` — does the output cover the required aspects?

- **Span:** `model_call`.
- **Input:** `metadata.input` + `metadata.output` (both split into sentences).
- **Output:** `score` — 0–1 float, mean over input sentences of best cosine
  match in the output (`None` unless both input and output have sentences).
- **Model:** `all-MiniLM-L6-v2` (embeddings).
- **How:** embed input + output sentences; score = mean over input sentences of
  best cosine match in the output (**input-coverage proxy**).
- **Note:** weakest proxy — measures coverage, not declared *requirements* (see
  `umbrella_1.md` open decisions).

| Alternative model | Size | Why consider it | Trade-off |
|---|---|---|---|
| `all-mpnet-base-v2` | 110M | best-in-class general SBERT, 768-dim | 5× slower |
| `BAAI/bge-base-en-v1.5` | 109M | top MTEB retrieval scores | needs query/passage prefixes |
| `thenlper/gte-base` | 109M | strong, no prefix needed | larger |
| schema-derived (no model) | — | for structured outputs, completeness = required-field presence | only structured outputs |
| LLM-judge | API | "list required aspects, check each present" | cost |

---

## 4. Umbrella 2 — Retrieval quality (model-based)

### 4.1 `context_relevance` — are retrieved chunks relevant to the query?

- **Span:** `retrieval`.
- **Input:** `metadata.query` + `metadata.chunks[].text`.
- **Output:** `score` — 0–1 float, **mean** per-chunk relevance (`None` when the
  query is empty or there are no chunks).
- **Model:** `cross-encoder/ms-marco-MiniLM-L-6-v2` (reranker).
- **How:** score each `(query, chunk)` pair → sigmoid to 0–1 → **mean across
  chunks**. This is literally the reranking task the model was trained on.
- **Evidence:** per-chunk scores in `metric_meta`.

| Alternative model | Size | Why consider it | Trade-off |
|---|---|---|---|
| `cross-encoder/ms-marco-MiniLM-L-12-v2` | ~33M | deeper, higher reranking accuracy | ~2× latency |
| **`BAAI/bge-reranker-base`** | 278M | SOTA-class reranker, multilingual | larger |
| `BAAI/bge-reranker-large` | 560M | strongest open reranker | heavy, GPU preferred |
| `mixedbread-ai/mxbai-rerank-base-v1` | 184M | strong recent reranker | larger |
| LLM-judge (RAGAS context_relevancy) | API | reasons about relevance | cost |

### 4.2 `chunk_utilization` — what fraction of chunks did the answer use?

- **Span:** `retrieval` (reads the same-trace `model_call` answer, cross-span).
- **Input:** `metadata.chunks[].text` + the trace's `model_call` `metadata.output`.
- **Output:** `score` — 0–1 float, used/total chunks (`None` when chunks or the
  answer are missing).
- **Model:** `all-MiniLM-L6-v2` (embeddings).
- **How:** chunk counts as "used" if best cosine vs any answer sentence ≥ **0.5**;
  score = used/total. Uses the same-trace `model_call` answer (cross-span).
- **Alternatives:** same embedder ladder as completeness; **lexical n-gram
  overlap** (cheaper, more literal "did this text appear"); LLM-judge attribution
  ("which chunks support each claim").

---

## 5. Umbrellas 3 & 4 — Mechanical (no models)

These metrics are deterministic functions over span metadata — **there is no
model to benchmark.** Listed here for completeness; their correctness is
validated by golden test cases, not P/R/F1.

| Metric | Umbrella | Span | Input (metadata) | Output | How computed | "Eval" =  |
|---|---|---|---|---|---|---|
| `data_completeness` | 3 | validation / skill_exec | `input` record, or `records_processed` + `batch_size` | `score` 0–1 (`None` if absent) | non-null field fraction / records_processed÷batch_size | golden records |
| `data_accuracy` | 3 | validation | `output.errors` + `input` (field count) | `score` 0–1 (`None` if no `errors`); errors → meta | 1 − errors/field_count | golden records |
| `schema_conformance` | 4 | validation / model_call | `valid` flag, or declared `schema`/`expected_schema`/`response_schema` + `output` | `ratio` 1.0/0.0 (`None` if no schema) | recorded `valid` flag / required-keys present (→ JSON-Schema Draft7 in v2) | golden (payload, schema)→verdict |
| `format_correctness` | 4 | model_call / tool_call / validation | `output` (or `response`) + optional `output_format`/`format` | `ratio` 1.0/0.0 (`None` if empty) | JSON parses / declared-format check | golden payloads |
| `constraint_satisfaction` | 4 | any span | `metadata.constraints` + `output` | `ratio` 1.0/0.0 (`None` if no constraints); violated/unchecked → meta | `metadata.constraints` convention checks | golden (output, constraints) |
| `tool_call_validity` | 4 | tool_call | `tool`, `request`, `response`, `span_status` | `ratio` 1.0/0.0; failed reasons → meta | name/args/response well-formedness | golden tool calls |

> Note: when umbrella 4's v2 (colleague's JSON-Schema validator) lands, schema
> conformance is still deterministic — its "eval" is a fixed suite of
> (payload, schema) → expected-verdict cases, ideal for unit tests in CI.

---

## 6. Evaluation plan — P/R/F1 for the 5 model-based metrics

Mirrors `PII/eval/benchmark_models.py`: load a labeled dataset, run each candidate
model, compare predictions to gold, print per-metric precision/recall/F1 + latency.

### 6.1 Turning a 0–1 score into a P/R/F1 prediction

These metrics output floats; P/R/F1 needs binary predictions. Two standard moves:

1. **Threshold sweep** — pick the decision threshold that maximizes F1 on a held-out
   split; report P/R/F1 at that threshold **plus threshold-free AUROC/AUPRC** so
   the comparison doesn't hinge on one cutoff.
2. **Correlation (recommended companion)** — for graded scores, Spearman/Pearson
   correlation against human ratings is the field-standard metric (SummEval,
   RAGAS). We report both: P/R/F1 (what was asked) and correlation (what the
   literature uses).

### 6.2 Candidate labeled datasets, per metric

Each semantic metric needs its **own** ground truth:

| Metric | Public datasets | Label shape |
|---|---|---|
| `faithfulness` | **RAGTruth**, HaluEval, FEVER, SummEac/SummEval (consistency) | (context, answer) → faithful / hallucinated |
| `coherence` | SummEval (coherence dim), CoLA-adjacent | text → coherence rating |
| `completeness` | SummEval (relevance/coverage), QAG sets | (source, summary) → coverage rating |
| `context_relevance` | **MS MARCO**, BEIR, TREC-DL | (query, passage) → relevant / not |
| `chunk_utilization` | (rare) RAGTruth attribution, synthetic | (chunks, answer) → used set |

Alternatively, a **single synthetic eval set** generated from a source corpus
(e.g. the Kaggle dataset explored earlier) with an LLM producing
faithful/unfaithful, complete/incomplete, relevant/irrelevant variants — one
dataset, controlled labels, all five metrics in one harness.

### 6.3 Harness design (planned: `signal-workers/eval/quality/benchmark_scorers.py`)

```
for each metric:
    for each candidate model:
        scores = scorer.score(dataset)           # reuse QualityScorer
        sweep threshold → best-F1, AUROC, AUPRC
        spearman(scores, gold_ratings)
    emit table: model · P · R · F1 · AUROC · corr · ms/example
    → docs/QUALITY_BENCHMARK_RESULTS.md  (house style)
```

The harness reuses the production `QualityScorer` implementations directly, so the
numbers reflect exactly what the lens computes.

### 6.4 Open decisions (block the eval run — see questions)

1. **Dataset source** — public per-metric benchmarks, one synthetic generated set,
   or an existing labeled set you already have.
2. **Scope** — all 5 semantic metrics, or start deep on `faithfulness` (the
   flagship / hardest) and expand.
3. Mechanical metrics — confirm they're out of the *model* benchmark (golden tests
   instead), as argued in §1.
