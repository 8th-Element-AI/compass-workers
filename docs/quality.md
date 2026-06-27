# Quality Lens — Architecture

11 metrics: 5 semantic (NLI + embedding + relevance cross-encoder) + 6 mechanical (read from span metadata, no ML). Two operating modes: full semantic, or mechanical-only (no torch, no GPU, fast).

## 1. Position

```mermaid
flowchart LR
  ch[(compass_raw_spans)] -->|"poll · partition_id ∈ owned"| qual["QualityWorker<br/>StatefulSet 1–16 pods"]
  pg_th[(thresholds where category='quality'<br/>AND is_active=true)] --> tc["ToggleCache"]
  qual -.-> tc
  qual -->|INSERT compass_derived_metrics| ch_der[(compass_derived_metrics)]

  subgraph models["Lazy semantic models<br/>(only if COMPASS_QUALITY_SEMANTIC=1)"]
    nli["NLI cross-encoder<br/>(faithfulness, coherence)"]
    emb["Sentence embedder<br/>(completeness, chunk_utilization)"]
    rel["Relevance cross-encoder<br/>(context_relevance)"]
  end
  qual -.-> models
```

## 2. Metrics

```mermaid
flowchart TB
  subgraph mech["Mechanical · always emit"]
    fc["format_correctness (output_bearing)"]
    sc["schema_conformance (schema_checked)"]
    tcv["tool_call_validity (tool_op)"]
    da["data_accuracy (validated_op)"]
    dc["data_completeness (data_op)"]
    cstr["constraint_satisfaction (any_span)"]
  end
  subgraph sem["Semantic · only if COMPASS_QUALITY_SEMANTIC=1"]
    faith["faithfulness (llm_call) ★"]
    coh["coherence (llm_call) ★"]
    comp["completeness (llm_call)"]
    crel["context_relevance (retrieval_op) ★"]
    cu["chunk_utilization (retrieval_op)"]
  end
```

★ = `threshold=True`. Mechanical metrics are all also `threshold=True`.

## 3. Per-batch flow

```mermaid
flowchart TD
  spans["batch of N spans"] --> ans_map["build trace_id → output map<br/>(retrieval needs the answer<br/>its chunks fed into; best-effort<br/>within batch)"]
  ans_map --> mode{"COMPASS_QUALITY_SEMANTIC?"}
  mode -->|0 / disabled| mech_only["skip semantic prefill<br/>no models loaded"]
  mode -->|1| sample{"_sampled(span)?<br/>(deterministic hash<br/>on span_id)"}
  sample -->|"miss (1-sample_rate)%"| skip_sem["skip semantic for this span<br/>(scores stay None)"]
  sample -->|hit| collect["collect scoring jobs:<br/>generation (input, output)<br/>retrieval (query, chunks, answer)<br/>deduped by content hash<br/>against LRU cache"]
  collect --> gen_run["scorer.score_generation(jobs)<br/>one NLI + embedding forward pass<br/>across all unique generation jobs"]
  collect --> ret_run["scorer.score_retrieval(jobs)<br/>one relevance + embedding forward pass<br/>across all unique retrieval jobs"]
  gen_run --> cache_w["LRU put"]
  ret_run --> cache_w
  cache_w --> sw_pb["super().process_batch(spans)<br/>= SpecWorker per-span loop"]
  mech_only --> sw_pb
  skip_sem --> sw_pb
  sw_pb --> ctx["build_context(span):<br/>read mechanical fields from metadata<br/>+ read semantic scores from LRU cache (if any)"]
  ctx --> emit["emit rows surviving both gates"]
```

### Sampling

`COMPASS_QUALITY_SAMPLE=0.2` ⇒ score a deterministic **20% of spans** based on `hash(span_id) % 100 < 20`. Same set across reruns. Mechanical metrics emit at 100% — only semantic models pay the cost.

## 4. Scorer pipeline (`quality_observability` package)

```mermaid
flowchart LR
  inp["(input, output)"] --> split_in["split_sentences (input)"]
  inp --> split_out["split_sentences (output)"]
  split_in --> nli_pair["NLI pairs:<br/>premise = input (capped at premise_max_chars),<br/>hypothesis = each output sentence"]
  split_out --> nli_pair
  nli_pair --> nli_run["NLI cross-encoder forward pass"]
  nli_run --> faith_out["faithfulness = mean(entail_probs)"]
  nli_run --> coh_out["coherence = 1 - mean(contradiction_probs)"]
  split_in --> emb["sentence embedder"]
  split_out --> emb
  emb --> comp_out["completeness = coverage(in_embs, out_embs)"]

  query["(query, chunks, answer?)"] --> rel_run["relevance cross-encoder<br/>each chunk vs query"]
  rel_run --> rel_out["context_relevance = mean(sigmoid(logit))"]
  query --> emb_ret["embedder on chunks + answer"]
  emb_ret --> cu_out["chunk_utilization = fraction of chunks<br/>whose best cosine match against answer<br/>≥ chunk_used_cos"]
```

One forward pass per model **across the entire batch's deduped jobs**. A batch of 5,000 spans with 100 unique system prompts becomes 100 generation jobs across one NLI forward + one embedder forward, not 5,000.

| Recipe knob | Default | Effect |
|---|---|---|
| `premise_max_chars` | 2000 | Cap on input fed to NLI as premise |
| `max_sents` | 10 | Cap on sentences kept per side |
| `sent_min_chars` | 3 | Min sentence length kept |
| `chunk_used_cos` | 0.5 | Cosine threshold for "answer used this chunk" |

## 5. Mechanical metrics (no ML)

| Metric | What it reads | When |
|---|---|---|
| `format_correctness` | `metadata.format_valid` bool | Output-bearing spans |
| `schema_conformance` | Compares `metadata.output` to `metadata.expected_schema` (or `.response_schema` / `.schema`) | Model_call with declared schema or validation span |
| `tool_call_validity` | `metadata.tool_call_valid` bool | tool_call spans |
| `data_accuracy` | `metadata.valid` + `metadata.errors[]` | validation spans |
| `data_completeness` | `metadata.completeness_ratio` | validation / skill_exec |
| `constraint_satisfaction` | `metadata.constraints` (rule object) — only computed when present | any span |

All cheap — no models needed. The Quality worker can run in mechanical-only mode (`COMPASS_QUALITY_SEMANTIC=0`) with the bare `:base` image; the heavyweight `:quality` image is only for full semantic scoring.

## 6. Lazy model loading + health check

```mermaid
flowchart TD
  start["pod start"] --> health["_verify_models_ready:<br/>check NLI / embedding / relevance<br/>directories exist on disk"]
  health -->|missing| crash["FileNotFoundError →<br/>CrashLoopBackOff"]
  health -->|all present| ready["readiness=True"]
  ready --> first["first batch with semantic job"]
  first --> load["scorer.nli.load() / .emb.load() / .rel.load()<br/>on first use<br/>(via LocalScorer)"]
  load --> serve["forward passes"]
```

The startup health check catches "Quality silently emits zero semantic rows because models failed to load" — fails loud at boot instead of silently never emitting.

## 7. Caching

| Cache | Size | Eviction | Why |
|---|---|---|---|
| ToggleCache | small | TTL 300s | Standard |
| Scoring LRU (`self._cache`) | `COMPASS_QUALITY_CACHE_MAX` = 20,000 | LRU, thread-safe | Same prompts repeat across thousands of spans |
| Model weights | on disk + RAM | pod lifetime | Bake-into-image |

Cache key = `sha256(text or json blob)[:16]`. The LRU is shared across generation and retrieval — different shapes hashed independently.

## 8. Topology + scaling

```mermaid
flowchart LR
  ss["StatefulSet compass-worker-quality<br/>replicas=N (1..16)"] --> pods["safety-0..N"]
  pods --> ch[(compass_raw_spans)]
  pods --> wc[(worker_checkpoints<br/>lens='quality', partition_key='slot:N')]
```

Same partition model as Safety. `cityHash64(trace_id) % 16` → slot ownership → per-slot checkpoints. Scale with `scale-quality.ps1 -Replicas N`.

| Pods | Slots/pod | Use |
|---|---|---|
| 1 | 16 | mechanical-only mode |
| 2 | 8 | low semantic load |
| 4 | 4 | typical prod with sampling |
| 8 | 2 | high semantic load |
| 16 | 1 | max — every slot has its own pod |

| Resources | Request | Limit |
|---|---|---|
| CPU | 1000m (CPU-only inference) | 4000m (NLI on CPU is the bottleneck) |
| Memory | 4Gi | 6Gi |
| GPU | optional — set `COMPASS_QUALITY_DEVICE=cuda` if available |

First-batch latency on CPU: cold NLI load + embedder load + relevance load = 5–15 s. Once warm, ~200 ms / job dominated by NLI forward.

## 9. Operating modes

```mermaid
flowchart TD
  env{"COMPASS_QUALITY_SEMANTIC?"}
  env -->|0| mech_mode["Mechanical-only<br/>image = compass-worker:base<br/>resources = 256-512Mi<br/>emits: 6 mechanical metrics<br/>no models, no torch"]
  env -->|1| sem_mode["Full semantic<br/>image = compass-worker:quality (~5.5 GB)<br/>resources = 4-6 GiB<br/>emits: all 11 metrics<br/>NLI + embedder + relevance loaded lazily"]
```

Mechanical-only is the fast lane for "is the structure right" coverage. Semantic adds "is the content right".

## 10. Failure modes

| Failure | Outcome |
|---|---|
| Model directory missing (image bake error) | Startup health check raises → CrashLoopBackOff → roll image |
| NLI returns NaN | `score_generation` returns None for that field → engine skips emit for that metric |
| Scorer batch OOM | Pod killed → restart. Reduce `COMPASS_QUALITY_BATCH` (default 32) or scale pods |
| Cache lock contention under high concurrency | Thread-safe OrderedDict with lock; minor cost. Visible as `compass_worker_batch_duration_seconds` p95 widening |
| Sampling makes a span's score never available | By design — `_sampled` is deterministic, so reruns produce the same set. Operators can lower `COMPASS_QUALITY_SAMPLE` to 1.0 to score everything |

## 11. Adding a quality metric

Mechanical:
- Add MetricSpec to `SPECS`, declare the metadata read in `build_context`.
- No image rebuild needed (mechanical-only path).

Semantic:
- Implement the scoring recipe in `quality_observability.pipeline`.
- Cache the result in `build_context` and route to ctx field.
- Declare MetricSpec.
- May require a new model artifact — update `Dockerfile.quality` to include it.

In both cases: redeploy reconciler first to update `metric_catalog` + thresholds, then redeploy quality.
