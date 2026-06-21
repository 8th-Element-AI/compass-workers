# Quality Observability Runtime

Standalone scoring package for **quality observability**. Returns per-metric
scores for `faithfulness`, `coherence`, `completeness`, `context_relevance`,
and `chunk_utilization` over LLM input/output text and retrieved chunks.
Used by the Signal Quality lens for the corresponding metrics; also usable
standalone via CLI or Python API.

> **This is not an evaluation harness.** It returns numeric quality scores
> so your application can populate dashboards and threshold alerts. It does
> not block, rewrite, or accept/reject text.

## Pipeline

```
(input, output) ──► NLI cross-encoder        ──► faithfulness  (entailment)
                                              ─► coherence     (1 − contradiction)
                ──► sentence embedder        ──► completeness  (coverage)

(query, chunks, answer?) ──► relevance cross-encoder  ──► context_relevance
                          ──► sentence embedder       ──► chunk_utilization
```

All three models lazy-load on first scoring call. A LocalScorer that's
only ever asked for generation scoring never instantiates the relevance
model, and vice versa — same pattern as `toxicity_observability`.

---

## What's in this repo

```
quality/
├── pyproject.toml
├── README.md
├── configs/
│   └── runtime.yaml                  model repos + recipe constants + runtime knobs
├── quality_observability/
│   ├── __init__.py                   public exports
│   ├── cli.py                        `quality-score` CLI
│   ├── config.py                     YAML loader + resolve_path
│   ├── constants.py                  default recipe constants
│   ├── text.py                       split_sentences, normalize_output
│   ├── scorer.py                     QualityScorer interface + StaticScorer
│   ├── models.py                     NLIModel, EmbeddingModel, RelevanceModel
│   ├── pipeline.py                   LocalScorer (the main entrypoint)
│   └── download.py                   HF model download for `quality-score download`
└── eval/
    ├── benchmark_faithfulness.py     HaluEval QA — NLI family comparison
    ├── benchmark_relevance.py        MS MARCO v1.1 — reranker comparison
    └── benchmark_summeval.py         SummEval — coherence + completeness
```

---

## Installation

```bash
pip install -e .
```

Pulls in `torch>=2.2`, `transformers`, `sentence-transformers`,
`huggingface_hub`, `numpy`, `pyyaml`.

For GPU inference, install a CUDA-matched torch build per the upstream
instructions; the package leaves device selection to the model adapters.

---

## Model download

Three artifacts:

| Artifact | HF repo (default) | Local path |
|---|---|---|
| NLI (DeBERTa) | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | `models/nli` |
| Embedding (MiniLM) | `sentence-transformers/all-MiniLM-L6-v2` | `models/embedding` |
| Relevance (ms-marco MiniLM) | `cross-encoder/ms-marco-MiniLM-L-6-v2` | `models/relevance` |

```bash
quality-score download --config configs/runtime.yaml
```

Models land under `quality/models/...` by default. To bake into a Docker
image, run the download in a build stage and ship absolute `local_path`
values pointing at `/opt/models/...`.

---

## Quick start (Python)

```python
from quality_observability import LocalScorer

# Lazy — first scoring call warms the model(s) needed.
scorer = LocalScorer("configs/runtime.yaml")

# Generation: (input, output) -> {faithfulness, coherence, completeness}
[res] = scorer.score_generation([
    ("Paris is the capital of France.",
     "Paris, capital of France, sits on the Seine."),
])
print(res["faithfulness"], res["coherence"], res["completeness"])

# Retrieval: (query, chunks, answer?) -> {context_relevance, chunk_utilization}
[res] = scorer.score_retrieval([
    ("What is the capital of France?",
     ["Paris is the capital of France.", "Rome is the capital of Italy."],
     "Paris is the capital."),
])
print(res["context_relevance"], res["chunk_utilization"])
```

### Config dict (used by signal-workers)

```python
clf = LocalScorer(config_dict={
    "models": {
        "nli":       {"local_path": "/opt/models/nli"},
        "embedding": {"local_path": "/opt/models/embedding"},
        "relevance": {"local_path": "/opt/models/relevance"},
    },
    "recipes": {
        "premise_max_chars": 2000,
        "max_sents":         10,
        "sent_min_chars":    3,
        "chunk_used_cos":    0.5,
    },
    "runtime": {
        "device":     "cpu",
        "batch_size": 32,
    },
})
```

### Override runtime kwargs

```python
clf = LocalScorer("configs/runtime.yaml", device="cuda", batch_size=64)
```

---

## CLI

```bash
# Download
quality-score download --config configs/runtime.yaml

# Score a generation pair
quality-score generation \
  --input  "Paris is the capital of France." \
  --output "Paris is in France and is the capital."

# Score a retrieval triple
quality-score retrieval \
  --query  "What is the capital of France?" \
  --chunks '["Paris is the capital.", "Rome is the capital of Italy."]' \
  --answer "Paris."

# Batch via stdin (JSON array or JSONL)
echo '[{"input":"...","output":"..."}]' | quality-score generation
```

---

## Output schema

`score_generation` returns one dict per job:

```json
{
  "faithfulness": 0.9012,
  "coherence":    0.8771,
  "completeness": 0.8400,
  "meta": {
    "out_sents":   3,
    "min_entail":  0.8120
  }
}
```

`score_retrieval` returns one dict per job:

```json
{
  "context_relevance": 0.91,
  "chunk_utilization": 0.50,
  "meta": {
    "chunks": 2,
    "rel":    [0.95, 0.12],
    "used":   1
  }
}
```

Any field may be `null` when the recipe's preconditions don't hold (no
input, single-sentence output, no answer to measure utilization against).
The Quality lens treats `null` as "nothing to emit" and skips that row.

---

## Configuration

`configs/runtime.yaml`:

```yaml
models:
  nli:
    repo_id:    "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"
    local_path: "models/nli"
  embedding:
    repo_id:    "sentence-transformers/all-MiniLM-L6-v2"
    local_path: "models/embedding"
  relevance:
    repo_id:    "cross-encoder/ms-marco-MiniLM-L-6-v2"
    local_path: "models/relevance"

recipes:
  premise_max_chars: 2000
  max_sents:         10
  sent_min_chars:    3
  chunk_used_cos:    0.5

runtime:
  device:     "cpu"
  batch_size: 32
```

| Recipe knob | Meaning | Tuning |
|---|---|---|
| `premise_max_chars` | Cap on input fed to NLI as premise | Lower → faster, may drop recall on long contexts |
| `max_sents` | Cap on sentences kept per side | Lower → fewer NLI pairs per span |
| `sent_min_chars` | Min sentence length kept after split | Higher → drop more fragments |
| `chunk_used_cos` | Cosine threshold for "answer used this chunk" | Calibrated against `SIGNAL_QUALITY_SAMPLE` traces |

| Runtime knob | Meaning |
|---|---|
| `device` | `cpu` or `cuda` — passed to each model adapter |
| `batch_size` | Forward-pass width for NLI / embedding / relevance |

---

## Use inside Signal Workers

The Quality lens imports this as `quality_observability`:

```python
from quality_observability import LocalScorer
scorer = LocalScorer(config_dict={...})  # built from signal-worker Settings
```

The worker calls the **batched** API directly so a whole batch of spans
shares one forward pass per model:

```python
gen = scorer.score_generation(generation_jobs)   # all spans in the batch
ret = scorer.score_retrieval(retrieval_jobs)
```

Lazy access still applies — a worker with only `faithfulness` toggles
active never instantiates the relevance model.

For offline `--csv` validation, inject `StaticScorer()` instead of
`LocalScorer` — same interface, no torch required.

---

## Evals

```bash
python eval/benchmark_faithfulness.py --max-records 300
python eval/benchmark_relevance.py    --max-queries 400
python eval/benchmark_summeval.py
```

Each script downloads its labeled dataset on first run (cached under
`eval/data/` for benchmark_faithfulness), runs each candidate model
through the **exact production recipe helpers** (`split_sentences`,
`normalize_output`, `PREMISE_MAX_CHARS`), and reports
P/R/F1 + AUROC/AUPRC + per-pair latency. Used to pick the defaults in
`configs/runtime.yaml`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Model files missing at `local_path` | Forgot to run `quality-score download` | Run the download CLI, or rebuild the Quality image |
| `HfHubHTTPError: 401 Client Error` | Private repo + missing `HF_TOKEN` | Set `HF_TOKEN` before `download` |
| First `score_*()` call slow (~5–10 s) | Lazy load on CPU | Warm up at startup; subsequent calls are fast |
| `faithfulness=null` everywhere | Empty / very short inputs in the batch | Expected — the recipe needs a non-empty premise |
| `coherence=null` everywhere | Single-sentence outputs | Expected — coherence needs ≥ 2 sentences |
| Sentence-transformers downloads at runtime | Pointed `local_path` at a HF id, not a local dir | Run download first, point YAML at the directory |