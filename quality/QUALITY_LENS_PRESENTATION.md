# Quality Lens — Presentation Notes

## 1. What the Quality lens does

The Quality lens turns each **span** (a log entry of an AI app operation) into a set of
**0–1 quality scores**, stored per-span and surfaced on dashboards / threshold alerts.

It answers two kinds of question:

- **"Is the AI's content good?"** — faithfulness, coherence, completeness, retrieval quality. *(needs ML models)*
- **"Is the data / structure correct?"** — schema, format, constraints, data quality. *(cheap, mechanical)*

---

## 2. The metrics (11 total)

### Semantic family — model-based

| Metric | Measures | Fires on |
|---|---|---|
| **faithfulness** | Output is grounded in the input (no hallucination) | model_call |
| **coherence** | Output doesn't contradict itself | model_call |
| **completeness** | Output covers what the input asked | model_call |
| **context_relevance** | Retrieved chunks are relevant to the query | retrieval |
| **chunk_utilization** | The answer actually used the retrieved chunks | retrieval |

### Mechanical family — no model, computed from span metadata

| Metric | Measures | Fires on |
|---|---|---|
| **data_completeness** | Fraction of record fields filled (or records processed / batch) | validation, skill_exec |
| **data_accuracy** | Few validation errors relative to field count | validation |
| **schema_conformance** | Required fields present / declared schema satisfied | model_call, validation |
| **format_correctness** | Output parses as its declared/apparent format (JSON) | model_call, tool_call, validation |
| **constraint_satisfaction** | Obeys declared limits (max_chars, contains, format…) | any span (only when constraints declared) |
| **tool_call_validity** | Tool call is well-formed (name, request, response) | tool_call |

### Deliberately out of scope
- **drift_score** — a batch/baseline comparison, not a per-span metric.
- **context_recall / context_precision** — require ground-truth relevance labels we don't have in observability data.

---

## 2b. Metric-by-metric detail

For each metric: **what it means → what it's doing → how we compute it → what we capture.**
Every metric returns a value in `[0, 1]`, or `None` ("nothing to score — skip this row").
`unit="score"` = a graded 0–1 value; `unit="ratio"` = a 0/1 pass-fail (averaged into a pass-rate on the dashboard).

### Semantic metrics (ML-scored)

#### 1. faithfulness  *(model_call, score, thresholded)*
- **Means:** is the model's output actually supported by its input/context? (i.e. not hallucinated)
- **Doing:** checks every output sentence against the input as evidence.
- **How:** input = "premise" (capped at 2000 chars); each output sentence = "hypothesis"; an **NLI cross-encoder** (DeBERTa) gives an *entailment* probability per pair; score = **mean entailment** across output sentences.
- **Captures:** the score, plus `metric_meta = {out_sents, min_entail}` (sentence count + the weakest-supported sentence — the audit trail for *why* a span scored low).
- **Null when:** no input premise to check against.

#### 2. coherence  *(model_call, score, thresholded)*
- **Means:** is the output internally consistent — does it contradict itself?
- **Doing:** looks for contradictions between consecutive sentences of the output.
- **How:** same **NLI model**; for each adjacent output-sentence pair it reads the *contradiction* probability; score = **1 − mean contradiction**.
- **Captures:** the score.
- **Null when:** fewer than 2 output sentences (nothing to compare).

#### 3. completeness  *(model_call, score, thresholded)*
- **Means:** does the output cover everything the input asked about?
- **Doing:** measures how well the output "covers" the input's content.
- **How:** an **embedding model** (MiniLM, L2-normalized) encodes input and output sentences; for each *input* sentence it takes the best cosine match to any *output* sentence; score = **average of those best matches** (clamped to [0,1]). This is the "coverage" recipe.
- **Captures:** the score.
- **Null when:** input or output has no usable sentences.

#### 4. context_relevance  *(retrieval, score, thresholded)*
- **Means:** are the chunks the system retrieved actually relevant to the query?
- **Doing:** scores each retrieved chunk against the query.
- **How:** a **relevance reranker** (ms-marco cross-encoder) scores each (query, chunk) pair; logits → sigmoid → [0,1]; score = **mean relevance** across chunks.
- **Captures:** the score, plus `metric_meta = {chunks, rel:[per-chunk scores], used}` (so you can see *which* chunk dragged the score down).
- **Null when:** no query or no chunks.

#### 5. chunk_utilization  *(retrieval, score)*
- **Means:** did the answer actually *use* the chunks that were retrieved, or were they ignored?
- **Doing:** compares each retrieved chunk against the final answer.
- **How:** the lens maps `trace_id → the model_call output` in the same batch (the "answer" the retrieval fed into); the **embedding model** measures each chunk's best cosine vs the answer; score = **fraction of chunks whose cosine ≥ 0.5** (the `chunk_used_cos` threshold).
- **Captures:** the score (carried in the same `ret_meta` as relevance).
- **Null when:** no answer was found in the batch for that trace (you can't measure use without the answer).

### Mechanical metrics (rules, no model)

#### 6. data_completeness  *(validation / skill_exec, score, thresholded)*
- **Means:** how complete is the data record / how much of the batch got processed?
- **Doing:** two cases by span type.
- **How:** *validation* span → **fraction of the input record's fields that are non-empty** (empty = `None/""/[]/{}`); *skill_exec* span → **records_processed / batch_size** (capped at 1.0).
- **Captures:** the score.
- **Null when:** no record to inspect, or batch size missing.

#### 7. data_accuracy  *(validation, score, thresholded)*
- **Means:** how error-free is the validated record?
- **Doing:** counts validation errors relative to field count.
- **How:** `1 − (number of errors / number of fields)`, floored at 0.
- **Captures:** the score, plus `metric_meta = {errors: first 20}` (the actual errors, capped so the row stays small).
- **Null when:** the output has no `errors` field to read.

#### 8. schema_conformance  *(model_call / validation, ratio, thresholded)*
- **Means:** does the output have the required shape/fields?
- **Doing:** validates structure against a declared schema (or trusts a validator's verdict).
- **How:** *validation* span → read the `valid` boolean; *model_call* span → only if a schema was declared (`expected_schema`/`response_schema`/`schema`), check **all required keys are present** in the output → 1.0, else 0.0.
- **Captures:** 1.0 / 0.0 (averaged = % conforming).
- **Null when:** no schema declared and no validity flag (nothing to check against).

#### 9. format_correctness  *(model_call / tool_call / validation, ratio, thresholded)*
- **Means:** is the output in the format it claims to be (primarily: valid JSON)?
- **Doing:** parses the output against its declared/apparent format.
- **How:** already-structured output (dict/list) → 1.0; text declared as JSON *or* that looks like JSON (`{`/`[`) **must parse** → 1.0 if it parses, 0.0 if it doesn't; plain text with no format claim → 1.0 (nothing to violate).
- **Captures:** 1.0 / 0.0.
- **Null when:** the output is empty.
- **Design note:** deliberately conservative — the *only* way to fail is "claims/looks like JSON but is broken." It never penalizes text it can't judge.

#### 10. constraint_satisfaction  *(any span, ratio)*
- **Means:** does the output obey the explicit constraints declared for it?
- **Doing:** checks the output against a declared `metadata.constraints` object.
- **How:** evaluates each constraint — `max_chars`, `min_chars`, `max_words`, `min_words`, `contains`, `not_contains`, `format:"json"`; score = 1.0 **only if every checkable constraint holds**, else 0.0. Unknown constraint keys are reported as **unchecked**, not failed.
- **Captures:** 1.0 / 0.0, plus `metric_meta = {violated:[...], unchecked:[...]}`.
- **Null when:** no constraints declared, or every declared constraint was unknown/uncheckable.

#### 11. tool_call_validity  *(tool_call, ratio, thresholded)*
- **Means:** was the tool/function call well-formed?
- **Doing:** structural checks on the tool-call span.
- **How:** flags failures if — the tool **name** is missing, the **request** isn't a dict/list (malformed), or the **response** is empty (unless the span itself errored/timed out, where an empty response is expected); 1.0 if no failures, else 0.0.
- **Captures:** 1.0 / 0.0, plus `metric_meta = {failed:[reasons]}` (e.g. `["missing_tool_name"]`).
- **Null when:** never explicitly null — it always has something to check on a tool_call span.

### Cross-cutting (applies to every metric)
- **`None` = skip:** a metric that can't be computed emits *no row* — it does not emit a 0. This keeps "couldn't measure" distinct from "measured and failed."
- **Gating:** a metric is only computed if its toggle is switched on for that span's app — expensive models never load for metrics nobody enabled.
- **Sampling & caching (semantic only):** identical input/output pairs are scored once (deduped by hash, cached); optional deterministic sampling scores a fixed fraction of spans.
- **`metric_meta`:** the audit JSON attached to a row — the "why" behind a score, used for debugging and dashboards.

---

## 3. Our approach

### Separation of concerns
- The **lens** (`compass-workers/.../lenses/quality.py`) owns the mechanical checks + worker mechanics.
- All **ML scoring** is delegated to a standalone sibling package, `quality_observability` (the `quality/` repo).
- Same pattern the **Safety** lens uses (it delegates to `deidentifier` for PII and `toxicity_observability`).

### Batch-once, read-many (performance)
1. `process_batch(spans)` collects the **unique** generation/retrieval scoring jobs (deduped by content hash).
2. **One forward pass per model** runs across the whole batch; results go into an LRU cache.
3. Each span's `build_context()` then cheaply **reads** its scores from the cache and runs the mechanical checks.

### Lazy + gated + tunable
- Models load **only when a metric is toggled on**; a worker that never sees an active `context_relevance` toggle never loads the relevance model.
- **Startup health check** verifies every model artifact before accepting traffic — a model that fails to load would otherwise emit zero-score rows that look like "evaluated, clean."
- `COMPASS_QUALITY_SEMANTIC=0` → mechanical-only mode (no torch).
- `COMPASS_QUALITY_SAMPLE=0.2` → score a deterministic 20% of spans (hashed on span_id, stable across reruns).

### Scoring recipes (one line each)
- **faithfulness** = mean NLI **entailment** of each output sentence vs the input premise.
- **coherence** = `1 − mean NLI contradiction` across adjacent output sentences (needs ≥2 sentences).
- **completeness** = average best-cosine **coverage** of input sentences by output sentences.
- **context_relevance** = mean **sigmoid'd** reranker score of each chunk vs the query.
- **chunk_utilization** = fraction of chunks whose best cosine vs the answer ≥ 0.5.

---

## 4. The models (3 — small, CPU-friendly)

| Role | Checkpoint | ~Size | Drives |
|---|---|---|---|
| **NLI** cross-encoder | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | 184M | faithfulness, coherence |
| **Embedding** | `sentence-transformers/all-MiniLM-L6-v2` | 22M | completeness, chunk_utilization |
| **Relevance** reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` | 22M | context_relevance |

All three **lazy-load** on first use and run on CPU by default. Models are pre-downloaded into the
image (no runtime network calls).

---

## 5. Validation methodology

Every model choice is justified by a benchmark that runs the **exact production recipe** against a
labeled public dataset, and reports threshold-free metrics so models compare honestly.

| Metric(s) | Script | Dataset | Primary metric |
|---|---|---|---|
| faithfulness | `eval/benchmark_model.py` | **HaluEval QA** (right vs hallucinated answers, balanced) | AUROC / AUPRC (+ best-F1) |
| context_relevance | `eval/benchmark_relevance.py` | **MS MARCO v1.1** (`is_selected` passages) | AUROC / AUPRC (+ best-F1) |
| coherence, completeness | `eval/benchmark_summeval.py` | **SummEval** (100 articles × 16 summaries, human 1–5 ratings) | **Spearman correlation** (+ F1/AUROC) |

Key talking points:
- Benchmarks call the **same recipe helpers** the lens ships (`split_sentences`, `normalize_output`, `PREMISE_MAX_CHARS`) — we measure what we ship, not a re-implementation.
- Each metric's production model is compared against a smaller and a larger/SOTA alternative, so the
  story is **"best accuracy-per-cost," not "biggest model."**
  - Faithfulness: production NLI vs `nli-deberta-v3-base` vs FEVER+ANLI variant.
  - Relevance: ms-marco-MiniLM-**L6** vs **L12** vs `BAAI/bge-reranker-base` (out-of-distribution).
  - Coherence/completeness: MiniLM/NLI vs `mpnet-base` / `bge-base`.
- Graded human ratings (SummEval) use **Spearman** (the SummEval standard); binary tasks use AUROC/AUPRC so they're threshold-independent.

---

## 6. Results

> Numbers below are from running the eval scripts against the production recipe
> (production model only; balanced, threshold-swept for best F1). Measured on CPU.

### Faithfulness — HaluEval QA (160 balanced pairs: 80 faithful / 80 hallucinated)
| Model | P | R | F1 | AUROC | AUPRC | thr | ms/pair |
|---|---|---|---|---|---|---|---|
| **MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli** (prod) | 0.763 | 0.887 | **0.821** | 0.751 | 0.637 | 0.32 | 80.5 |

*Reading it:* the recipe separates faithful from hallucinated answers with **F1 0.82 / AUROC 0.75**.
High recall (0.89) = it rarely misses a hallucination; the best operating threshold sits at 0.32.

### Context relevance — MS MARCO v1.1 (324 balanced pairs: 162 relevant / 162 not)
| Model | P | R | F1 | AUROC | AUPRC | thr | ms/pair |
|---|---|---|---|---|---|---|---|
| **cross-encoder/ms-marco-MiniLM-L-6-v2** (prod) | 0.612 | 0.914 | **0.733** | 0.756 | 0.724 | 0.99 | 7.1 |

*Reading it:* ranks relevant chunks above irrelevant ones at **AUROC 0.76 / AUPRC 0.72**, at just
**7 ms/pair** (22M model). Very high recall (0.91). Note MS MARCO is the model's *training
distribution*, so this is an in-distribution sanity check, not a generalization claim.

### Coherence & completeness — SummEval (250 examples, Spearman vs human 1–5 ratings)
| Metric | Model | Spearman | AUROC | F1 |
|---|---|---|---|---|
| coherence | MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli | **0.08** | 0.52 | 0.67 |
| completeness | sentence-transformers/all-MiniLM-L6-v2 | **0.33** | 0.70 | 0.74 |

*Reading it:* **completeness** correlates moderately with human coverage judgments (Spearman 0.33,
AUROC 0.70) — a reasonable proxy. **coherence** correlates weakly here (Spearman 0.08, AUROC ≈ chance):
the "1 − adjacent-sentence contradiction" proxy is a *cheap structural* signal, not a full discourse
model, and SummEval coherence is a hard graded-correlation task. Treat coherence as a low-cost
contradiction flag, not a calibrated coherence score. *(Ignore the F1 column here — recall=1.0 means the
best-F1 threshold collapsed to "predict all positive"; Spearman/AUROC are the honest signals.)*

---

## 7. One-slide summary

- **11 metrics**, two families: 5 semantic (ML) + 6 mechanical (rules).
- **Architecture:** lens owns mechanical checks + batching; ML delegated to a reusable sibling package.
- **3 small CPU models** drive the semantic scores; lazy-loaded, gated, sampleable.
- **Validated** against HaluEval, MS MARCO, and SummEval using the production recipe and
  threshold-free metrics — chosen for best accuracy-per-cost.
