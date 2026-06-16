# Quality Lens — Model Benchmark Results

> Precision/recall/F1 (+ AUROC and, for graded datasets, Spearman correlation) of
> the **model-based** quality metrics against labeled public datasets, following
> the house pattern from `PII/eval/BENCHMARK_RESULTS.md`. Four of the five
> semantic metrics are benchmarked here; `chunk_utilization` has no standard
> labeled dataset (see §6). The 6 mechanical metrics (umbrellas 3–4) have no model
> and are out of scope — they get golden-case correctness tests instead.

> **Status (2026-06-16):** the faithfulness recommendation has been **adopted** —
> the production NLI model is now `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli`
> (shared by faithfulness + coherence), and `LocalScorer` now reads the NLI label
> order from the model config so any NLI model drops in. See the Adoption note.

---

## Executive summary

| Metric                      | Dataset       | Current model              | Current AUROC   | Best alternative                    | Best AUROC      | Verdict                                |
| --------------------------- | ------------- | -------------------------- | --------------- | ----------------------------------- | --------------- | -------------------------------------- |
| **faithfulness**      | HaluEval QA   | `nli-deberta-v3-xsmall`  | **0.406** | `DeBERTa-v3-base-mnli-fever-anli` | **0.739** | ✅**SWAPPED** (+33 pts, adopted) |
| **context_relevance** | MS MARCO v1.1 | `ms-marco-MiniLM-L-6-v2` | **0.746** | `bge-reranker-base`               | 0.754           | **KEEP** (alternatives tied)     |
| **coherence**         | SummEval      | `nli-deberta-v3-xsmall`  | **0.566** | `nli-deberta-v3-base`             | 0.606           | **RECIPE-LIMITED** (all weak)    |
| **completeness**      | SummEval      | `all-MiniLM-L6-v2`       | **0.615** | `all-mpnet-base-v2`               | 0.637           | **KEEP** (recipe is the ceiling) |
| chunk_utilization           | —            | `all-MiniLM-L6-v2`       | not benchmarked | —                                  | —              | no standard dataset (§6)              |

**The eval cleanly separates the metrics into two groups:**

1. **Model-fixable** — `faithfulness` (a model swap is a huge win) and
   `context_relevance` (already good, alternatives don't justify their cost). For
   these, picking the right model is the lever.
2. **Recipe-limited** — `coherence` and `completeness` score weakly (AUROC
   ~0.57–0.64) **regardless of model**. No model swap rescues them; the *recipe*
   (NLI-contradiction proxy / embedding-coverage proxy) is the ceiling. Improving
   these needs a better formulation (e.g. LLM-judge), not a bigger encoder.

---

## 1. faithfulness — HaluEval QA  →  ✅ MODEL SWAPPED (adopted)

200 records → 400 balanced pairs (grounded vs hallucinated answers, same context),
production faithfulness recipe.

| Model                                                      | Params | F1              | **AUROC** | AUPRC | P     | R     | ms/pair |
| ---------------------------------------------------------- | ------ | --------------- | --------------- | ----- | ----- | ----- | ------- |
| `cross-encoder/nli-deberta-v3-xsmall`                    | 22M    | 0.673           | **0.406** | 0.413 | 0.521 | 0.950 | 20.7    |
| `cross-encoder/nli-deberta-v3-base`                      | 184M   | 0.678           | **0.485** | 0.451 | 0.536 | 0.920 | 47.5    |
| **`MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli`** | 184M   | **0.792** | **0.739** | 0.614 | 0.682 | 0.945 | 28.5    |

**It's the training data, not the size.** The current xsmall model is **below
chance (0.406)**. Scaling the same generic-NLI family up 8× barely helped (0.485).
The **FEVER+ANLI fact-verification** model — same 184M — lifted AUROC to **0.739
(+33 pts)** and ran *faster* than the generic base. Faithfulness *is* fact
verification (claim + evidence → supported?), so a FEVER-trained model is
purpose-matched; generic MNLI/SNLI cross-encoders are not.

> Caveat: HaluEval grounded answers are often short noun phrases while
> hallucinated ones are full sentences — NLI scores bare noun phrases low, partly
> explaining the sub-chance current score. The relative comparison holds: the
> FEVER model overcomes the same confound on the same data with the same recipe.

---

## 2. context_relevance — MS MARCO v1.1  →  KEEP CURRENT

400 queries → balanced (query, passage) pairs (`is_selected` = relevant),
production relevance recipe (cross-encoder → sigmoid per pair).

| Model                                                        | Params | F1    | **AUROC** | AUPRC | P     | R     | ms/pair |
| ------------------------------------------------------------ | ------ | ----- | --------------- | ----- | ----- | ----- | ------- |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` **(current)** | 22M    | 0.721 | **0.746** | 0.720 | 0.658 | 0.798 | 14.6    |
| `cross-encoder/ms-marco-MiniLM-L-12-v2`                    | 33M    | 0.721 | 0.744           | 0.721 | 0.650 | 0.810 | 5.9     |
| `BAAI/bge-reranker-base`                                   | 278M   | 0.734 | **0.754** | 0.723 | 0.626 | 0.889 | 34.6    |

The current reranker is **already strong (0.746)** and the alternatives are
**within noise** (0.744–0.754). bge-reranker is +0.008 AUROC for 2.4× the latency
and 12× the params — not worth it. **Keep `ms-marco-MiniLM-L-6-v2`.**

> Note: MS MARCO is the *training distribution* for the ms-marco-* rerankers, so
> these are in-distribution numbers; a domain-shifted corpus may change the
> ranking. bge (out-of-distribution here) being only marginally ahead is
> reassuring for the current choice.

---

## 3. coherence — SummEval  →  RECIPE-LIMITED (all models weak)

1,600 (source, summary) examples, human coherence ratings. Score = `1 − mean adjacent-sentence NLI contradiction`. Primary metric is **Spearman** vs the human
rating (field standard); P/R/F1/AUROC on a median split.

| Model                                                       | Params | **Spearman** | AUROC           | F1    | ms/ex |
| ----------------------------------------------------------- | ------ | ------------------ | --------------- | ----- | ----- |
| `cross-encoder/nli-deberta-v3-xsmall` **(current)** | 22M    | 0.141              | 0.566           | 0.668 | 11.0  |
| `cross-encoder/nli-deberta-v3-base`                       | 184M   | **0.241**    | **0.606** | 0.669 | 21.5  |
| `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli`            | 184M   | 0.189              | 0.585           | 19.4  | —    |

**All three are weak** (Spearman 0.14–0.24, AUROC barely above 0.5). The
adjacent-sentence-contradiction recipe captures only a sliver of what humans call
coherence (it misses long-range structure, topic drift, and rambling that
contradicts nothing). The base model is marginally best, but **no model swap
fixes this** — the recipe is the limitation. To improve coherence, change the
*formulation* (LLM-judge for holistic flow, or add embedding-drift +
repetition-rate signals), not the encoder.

> Shared-model note: coherence uses the **same** NLI model as faithfulness
> (`SIGNAL_QUALITY_NLI_MODEL`). We optimized that model for faithfulness (where it
> mattered — +33 AUROC), so production now runs coherence on the FEVER model too.
> Since coherence is recipe-limited (all NLI models within ~0.04 AUROC of each
> other), sharing costs nothing meaningful here.

---

## 4. completeness — SummEval  →  KEEP CURRENT (recipe is the ceiling)

1,600 (source, summary) examples, human relevance/coverage ratings. Score =
embedding coverage of source sentences by the summary.

| Model                                    | Params | **Spearman** | AUROC           | F1    | ms/ex |
| ---------------------------------------- | ------ | ------------------ | --------------- | ----- | ----- |
| `all-MiniLM-L6-v2` **(current)** | 22M    | 0.216              | 0.615           | 0.721 | 12.4  |
| `all-mpnet-base-v2`                    | 110M   | **0.255**    | **0.637** | 0.727 | 91.4  |
| `BAAI/bge-base-en-v1.5`                | 109M   | 0.245              | 0.635           | 0.724 | 865.0 |

Weak across the board (Spearman 0.22–0.26). mpnet is marginally best but **7×
slower**; bge-base gave **no better signal at ~70× the latency** (865 ms/ex on
long source docs). The current MiniLM is the **best value** — the alternatives buy
+0.04 Spearman for a large latency hit. As with coherence, the embedding-coverage
*recipe* is the ceiling (it measures input-coverage, not declared requirements);
the model choice is second-order. **Keep MiniLM**; revisit the recipe
(schema-derived completeness for structured outputs, or LLM-judge) if higher
fidelity is needed.

---

## 5. Cross-metric reading

- **Faithfulness** was the one clear model win — and a big one. The FEVER model
  is now adopted in production (✅).
- **Relevance** is already solved by the current small reranker.
- **Coherence & completeness** are the honest weak spots: the eval shows their
  proxies are only loosely correlated with human judgment and that throwing bigger
  models at them doesn't help. They remain useful as *directional* signals but
  should not drive hard thresholds/alerts until the recipes improve.
- **Latency vs accuracy**: every "best alternative" except the FEVER faithfulness
  model traded large latency for ≤0.04 AUROC/Spearman. The small-model floor is
  well-chosen everywhere except faithfulness.

---

## 6. chunk_utilization — not benchmarked

No standard public dataset labels "which retrieved chunks did the answer actually
use." Options to benchmark it later: RAGTruth attribution annotations, or a
synthetic set (chunks with known used/unused status). Until then it stays a
heuristic (cosine ≥ 0.5 vs the answer) and is validated only by spot-checks.

---

## Adoption note (faithfulness) — ✅ done (2026-06-16)

Swapping in the FEVER model was **not just config**: the production `LocalScorer`
had hardcoded the NLI label order `(_NLI_CONTRA, _NLI_ENTAIL) = (0, 1)` (correct
only for `cross-encoder/nli-*`), while the MoritzLaurer model uses
`{0: entailment, 1: neutral, 2: contradiction}`.

**Implemented:** `LocalScorer.nli()` now reads `model.config.id2label` at load time
and resolves the entailment/contradiction column indices dynamically (verified:
the FEVER model resolves to `entail=0, contra=2`; the old cross-encoder family to
`entail=1, contra=0` — both score correctly). The default
`SIGNAL_QUALITY_NLI_MODEL` is now the FEVER model. This same fix lets any NLI model
drop in via config, including for coherence.

Deployment notes: the FEVER model is 184M vs the old 22M (~8× params), so first
load and per-span latency rise (~28 ms/pair vs ~21 in the benchmark);
`SIGNAL_QUALITY_SAMPLE` is the lever if needed. Any faithfulness thresholds set
against the old model are now invalid and must be recalibrated.

### Observed distribution shift (sample data, full re-run over 3,160 model_call spans)

Re-running the lens end-to-end (offline, identical compute) with the new model
shifts the NLI-based metrics and leaves the embedding-based one untouched — a
clean confirmation that only the NLI model changed:

| Metric       | Driving model         | Old mean (xsmall) | New mean (FEVER) | New p10 / p50 / p90 |
| ------------ | --------------------- | ----------------- | ---------------- | ------------------- |
| faithfulness | NLI                   | 0.2238            | **0.3258** | 0.01 / 0.29 / 0.76  |
| coherence    | NLI (shared)          | 0.4956            | **0.8391** | 0.58 / 0.85 / 1.00  |
| completeness | embedding (unchanged) | 0.4861            | 0.4861           | 0.17 / 0.56 / 0.86  |

- **faithfulness** mean rises and *spreads* (p10 0.01 → p90 0.76) — the FEVER model
  discriminates rather than clustering everything low as xsmall did.
- **coherence** jumps sharply (0.50 → 0.84): the FEVER model flags far less
  contradiction between unrelated adjacent sentences than the generic NLI.
- **completeness** is byte-identical — it never used the NLI model — confirming the
  swap is isolated to the NLI-driven metrics.

> These are synthetic-sample distributions (the numbers describe *what the new
> model emits on our test spans*, not its accuracy — accuracy is the HaluEval
> benchmark above). They underline the recalibration point: the coherence
> operating range in particular moved a lot.

---

## Operating points & calibration

P/R/F1 require binarizing the 0–1 score, so thresholds were swept for best F1;
AUROC/AUPRC (and Spearman) are the threshold-free, honest comparisons. Best-F1
thresholds vary wildly by model (e.g. FEVER faithfulness at 0.06, ms-marco
relevance at 1.0) because score distributions differ — production thresholds must
be **calibrated per model from observed data**, never carried across models or set
by intuition.

---

## Methodology

| Metric            | Dataset           | Source                    | Labels                      | n                      |
| ----------------- | ----------------- | ------------------------- | --------------------------- | ---------------------- |
| faithfulness      | HaluEval QA       | GitHub raw JSON           | grounded(1)/hallucinated(0) | 400 pairs              |
| context_relevance | MS MARCO v1.1 val | HF `microsoft/ms_marco` | is_selected(1/0)            | ~400 queries, balanced |
| coherence         | SummEval          | HF `mteb/summeval`      | human 1–5 → median split  | 1,600 (1,549 scorable) |
| completeness      | SummEval          | HF `mteb/summeval`      | human 1–5 → median split  | 1,600                  |

- **Recipes:** identical to `signal_worker/scorers.py` (the lens's real compute).
- **Label order:** read from each model's `config.id2label` at runtime.
- **Hardware:** Apple M-series CPU, no GPU. First-pass sample sizes; confirm on
  larger slices + a second dataset (RAGTruth) before final calibration.
- **Harnesses:** `eval/quality/benchmark_faithfulness.py`,
  `benchmark_relevance.py`, `benchmark_summeval.py`.

## How to reproduce

```bash
# faithfulness (curl avoids the macOS Python SSL-cert issue)
mkdir -p eval/quality/data
curl -sL https://raw.githubusercontent.com/RUCAIBox/HaluEval/main/data/qa_data.json \
     -o eval/quality/data/halueval_qa.json
python eval/quality/benchmark_faithfulness.py --max-records 200

# context_relevance (downloads MS MARCO parquet via huggingface_hub)
python eval/quality/benchmark_relevance.py --max-queries 400

# coherence + completeness (downloads SummEval parquet)
python eval/quality/benchmark_summeval.py
```

## Next steps

1. ~~**Adopt the FEVER model** for faithfulness + make the scorer read label order
   from config.~~ ✅ done (2026-06-16) — see Adoption note.
2. **Rework coherence & completeness** recipes (LLM-judge or composite signals) —
   model swaps won't move them.
3. **Confirm faithfulness on RAGTruth** + larger slices; add the HHEM hallucination
   model and an LLM-judge rung to the faithfulness table.
4. **Benchmark chunk_utilization** once an attribution-labeled set exists.
