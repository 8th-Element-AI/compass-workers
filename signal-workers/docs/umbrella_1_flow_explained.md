# Umbrella 1 — Quality Lens Flow (Explained)

> Companion to `umbrella_1_flow.png`. A demo-friendly walkthrough of how the
> Quality lens scores AI interactions, grounded in the actual code
> (`signal_worker/lenses/quality.py` and `signal_worker/scorers.py`).

## The one-sentence pitch

> "This pipeline watches every AI interaction our system logs, scores how
> *good* those answers were — are they truthful, do they make sense, are they
> complete — and rolls those scores up into live dashboards and alerts. All of
> it runs on cheap local models, no external API calls."

## The big picture (top to bottom)

Raw events flow in at the top, get scored in the middle (the yellow/orange
boxes), and come out as dashboard numbers at the bottom. Read it like an
assembly line.

---

## Walking through the boxes

**1. `signal_raw_spans` (the blue cylinder at top)**
This is the source database (ClickHouse). Every time something happens in our
AI app — a model is called, a tool is used, a document is retrieved — it gets
logged here as a "span." It's **immutable**: we only read from it, never change
it.

**2. `fetch_batch` (the gray arrow)**
The worker wakes up and asks: "What's new since I last looked?" It uses a
**watermark** — basically a bookmark — so it only grabs spans it hasn't
processed yet. It pulls them in batches.

**3. The yellow box — `quality.py` (the orchestrator)**
This is the brain that decides *what work needs doing*. Inside it:

- **Collect (input, output) pairs** — for each AI model call, grab what went in
  (the prompt) and what came out (the answer).
- **`sampled?`** — we don't always score everything. There's a knob
  (`SIGNAL_QUALITY_SAMPLE`) to score, say, only 20% of traffic to save compute.
  The choice is **deterministic by span_id** — the same span always gets the
  same yes/no answer, even if we re-run. (Code: `quality.py` lines 298–305.) If
  "no" → no scoring for that span.
- **Job key = hash(input):hash(output)** — the cost-saver. If the same
  prompt+answer shows up 100 times in a batch, we fingerprint it and **only
  score it once** (dedupe).
- **`in LRU cache?`** — have we already scored this exact pair recently? The LRU
  cache remembers recent results.
  - **Hit** → skip all the expensive model work, reuse the saved score (the long
    line down the left side).
  - **Miss** → send it to the scorers.

> Demo talking point: the sampling + dedupe + cache combo is what makes this
> affordable. Most spans never hit a model at all.

**4. The orange box — `scorers.py` (the actual ML scoring)**
This is where the small local AI models run. One **batched call per batch**
(efficient). It scores three things about each answer — see the deep dive below.

**5. `scores + evidence → content-hash LRU cache`**
The fresh scores get saved back into that cache — so next time the same content
appears, it's a cache hit.

**6. The second yellow box — `spec.py` (turning scores into metric rows)**

- **`build_context`** looks up the cached scores for this span.
- **3 MetricSpecs emit** the final rows: faithfulness, coherence, completeness —
  each with **evidence** attached (e.g. how many sentences, the weakest
  entailment score) so the number is auditable, not a black box.

**7. `signal_derived_metrics` (blue cylinder)**
The scored results land here — **one row per metric per span** (EAV = a flexible
"entity-attribute-value" layout). Written as **one bulk insert per batch**, then
the watermark advances so we don't re-process.

**8. `mv_agg_base` → `signal_aggregated_metrics`**
A **materialized view** automatically fires on insert (no scheduled job needed)
and rolls everything into **1-minute buckets**. Think: "average faithfulness per
minute."

**9. `avgMerge / quantilesTDigestMerge`**
At read time, those buckets get combined into final averages and percentiles.

**10. Dashboards / KPIs / signals (bottom)**
The numbers show up on dashboards, and **Postgres thresholds** decide when a
metric is bad enough to fire an alert ("a signal").

---

## Deep dive: the ML scoring (`scorers.py`)

### The three models powering this

The scorer (`LocalScorer`) loads three small, CPU-friendly models. They're
**lazy-loaded** — only pulled into memory the first time they're actually
needed, so a mechanical-only run never even imports torch.

| Role                | Model                                      | What it does                                                                                                                                      |
| ------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| **NLI**       | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli`    | Given two sentences (a "premise" and a "hypothesis"), says whether the second is*entailed by*, *contradicts*, or is *neutral* to the first. FEVER-trained — benchmarked best for faithfulness. |
| **Embedding** | `sentence-transformers/all-MiniLM-L6-v2` | Turns any sentence into a vector (a list of numbers) so we can measure how*similar* two sentences are in meaning.                               |
| **Relevance** | `cross-encoder/ms-marco-MiniLM-L-6-v2`   | Scores how relevant a passage is to a search query (used for the retrieval metrics, not the three below).                                         |

> Demo line: "These are tiny models — a few hundred MB, run on CPU. We're not
> calling GPT or Claude to grade answers; we use purpose-built scoring models,
> which is why it's cheap enough to run on live traffic."

A key concept: **NLI = Natural Language Inference.** It's a classifier trained to
answer "does sentence B logically follow from sentence A?" It outputs three
probabilities that add up to 1: **contradiction / entailment / neutral**. We use
a "cross-encoder," meaning it reads both sentences *together* (more accurate than
comparing them separately).

### Step 0: Prep work (before any model runs)

Two things happen to every answer first:

- **JSON flattening** (`normalize_output`): these models were trained on English
  prose, not raw JSON. So `{"status": "ok"}` becomes the sentence
  `"status is ok."` before scoring.
- **Sentence splitting** (`split_sentences`): the answer is chopped into
  individual sentences, capped at **10 per side** (`MAX_SENTS`) to keep the work
  bounded. Fragments under 3 characters are dropped.

### Faithfulness — "did it make stuff up?"

**The question:** Is every claim in the answer actually supported by the input
we gave the model?

**How it's done** (`score_generation`, lines 204–228):

1. Take the input text as the **premise** (truncated to 2,000 chars —
   `PREMISE_MAX_CHARS`).
2. Take **each sentence of the answer** as a **hypothesis**.
3. Run every (premise, answer-sentence) pair through the **NLI model**.
4. Grab the **entailment probability** for each pair — i.e. "how strongly does
   the input support this sentence?"
5. **Faithfulness score = the average entailment probability** across all answer
   sentences.

So if the answer has 5 sentences and 4 are well-supported by the input but 1 is
invented, that 5th sentence scores low on entailment and drags the average down.

**Evidence captured:** it also records `min_entail` — the single weakest
sentence's entailment score. That's the smoking gun: a high average but a very
low minimum means "mostly fine, but one sentence is unsupported."

### Coherence — "does it contradict itself?"

**The question:** Is the answer internally consistent, or does it say one thing
then the opposite?

**How it's done** (lines 209–231):

1. Take **adjacent pairs** of answer sentences: (sentence 1, sentence 2),
   (sentence 2, sentence 3), and so on.
2. Run each pair through the same **NLI model**, but this time read the
   **contradiction probability**.
3. **Coherence score = 1 − (average contradiction probability).**

High contradiction between neighboring sentences → low coherence. If the answer
has **fewer than 2 sentences**, there's nothing to compare, so the metric is
skipped (returns `None`, no row emitted).

> Note for the demo: it only checks *neighboring* sentences, not every possible
> pair. That's a deliberate cost trade-off — it catches local self-contradiction
> cheaply rather than doing an expensive all-pairs comparison.

### Completeness — "did it cover everything?"

**The question:** Does the answer actually address the topics in the input, or
did it skip parts?

This one uses the **embedding model**, not NLI (it's about topic coverage, not
logic).

**How it's done** (`_coverage`, lines 189–233):

1. Turn **each input sentence** and **each answer sentence** into vectors with
   the embedding model. These vectors are **L2-normalized**, which makes
   "dot product = cosine similarity" (a 0–1 measure of how close two meanings
   are).
2. For **each input sentence**, find its **best match** among the answer
   sentences (highest cosine similarity) — i.e. "did the answer address this
   point at all, and how well?"
3. **Completeness score = the average of those best-match scores**, clamped to
   0–1.

The intuition: if every point in the input has *some* sentence in the answer
that closely matches it, coverage is high. If an input topic has no good match
anywhere in the answer, it scores near zero and pulls the average down.

### Why these are called "proxies" (important honesty point)

The docstring is explicit: these are **documented proxies, coarser than an LLM
judge** (`scorers.py:16`). They don't "understand" the answer the way a big model
would — they're fast statistical approximations:

- Faithfulness can be fooled by paraphrasing that's true but not literally
  entailed.
- Completeness measures *semantic overlap*, not whether the answer is *correct*.

The design choice is **speed and cost over perfection** — and crucially, the
architecture lets you **swap the backend**. Because the Quality lens only depends
on the `QualityScorer` *interface*, you could drop in an LLM judge (e.g. Claude)
for the same three metrics without touching any of the metric definitions.
There's already a `StaticScorer` that returns fixed scores for offline testing.

> Strong closing line for the demo: "Today these run on small local models for
> cost. But the scoring backend is pluggable — if we want higher-fidelity
> grading on a critical subset, we can route those through a frontier LLM judge
> with zero changes to the rest of the pipeline."

### One-glance summary table

| Metric                 | Model used           | Input fed in                        | What we read out                     | Final score             |
| ---------------------- | -------------------- | ----------------------------------- | ------------------------------------ | ----------------------- |
| **Faithfulness** | NLI (deberta-base-fever-anli) | (input, each answer sentence)       | entailment probability               | mean entailment         |
| **Coherence**    | NLI (deberta-base-fever-anli) | (adjacent answer sentence pairs)    | contradiction probability            | 1 − mean contradiction |
| **Completeness** | Embedding (MiniLM)   | input & answer sentences as vectors | best cosine match per input sentence | mean of best matches    |

---

## The 3 themes to hit in your demo

1. **What it measures:** truthfulness (faithfulness), consistency (coherence),
   thoroughness (completeness) of AI answers — automatically, on live traffic.
2. **Why it's cheap:** sampling + dedupe + caching + small local models means we
   score a fraction of traffic and reuse results aggressively.
3. **Why it's trustworthy:** every score carries evidence, and the source data
   is immutable — you can always trace a dashboard number back to the exact span
   and reasoning.
