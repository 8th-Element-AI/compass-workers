# Safety Lens — Architecture

3 metrics. Heavy ML: Presidio NER for PII + 2 DeBERTa-class ONNX classifiers for prompt-injection and moderation. Runs as a partitioned StatefulSet with 1–16 replicas.

## 1. Position

```mermaid
flowchart LR
  ch[(compass_raw_spans)] -->|"poll · span_types=('model_call',)<br/>partition_id IN owned_slots"| safety["SafetyWorker<br/>StatefulSet 1–16 pods"]
  pg_th[(thresholds where category='safety'<br/>AND is_active=true)] --> tc["ToggleCache"]
  safety -.-> tc
  safety -->|"INSERT compass_derived_metrics<br/>dedup_token=safety:s{slot}:{ts}:{n}"| ch_der[(compass_derived_metrics)]

  subgraph models["Lazy-loaded models (in pod memory)"]
    pii_m["Presidio NER<br/>(deidentifier)"]
    pi_m["Prompt Injection BERT<br/>(toxicity_observability)"]
    mod_m["Moderation BERT<br/>(toxicity_observability)"]
  end
  safety -.-> models
```

## 2. Metrics

```mermaid
flowchart LR
  pii_metric["pii_detected (llm_call)"] --> pii_step["pii PrefillStep<br/>Presidio over input + output"]
  pi_metric["prompt_injection_detected (llm_call)"] --> pi_step["prompt_injection PrefillStep<br/>BERT over input only"]
  tox_metric["toxicity_detected (llm_call)"] --> mod_step["moderation PrefillStep<br/>BERT over input + output<br/>max score"]
```

All three are `threshold=True`. Pre-filter at CH: `span_types = ('model_call',)` — only LLM generation spans carry text worth analyzing.

## 3. Per-batch flow — PrefillStep pipeline

```mermaid
flowchart TD
  spans["batch of N spans (model_call only)"] --> stage1["Stage 1 gate:<br/>drop spans with no active safety toggle"]
  stage1 --> kept["kept spans (subset)"]
  kept --> pii_branch{"any kept span<br/>has active pii_detected toggle?"}
  pii_branch -->|no| skip_pii["skip PII step entirely<br/>(Presidio NOT loaded)"]
  pii_branch -->|yes| pii_run["PII PrefillStep:<br/>extract (input,output) texts<br/>dedup by content hash<br/>presidio.analyze on UNIQUE texts<br/>cache results"]
  skip_pii --> pi_branch
  pii_run --> pi_branch{"any kept span<br/>has active prompt_injection toggle?"}
  pi_branch -->|no| skip_pi["skip PI step<br/>(PI BERT NOT loaded)"]
  pi_branch -->|yes| pi_run["PI step:<br/>extract input only<br/>dedup<br/>BERT classify_batch<br/>cache"]
  skip_pi --> mod_branch
  pi_run --> mod_branch{"any kept span<br/>has active toxicity_detected toggle?"}
  mod_branch -->|no| skip_mod
  mod_branch -->|yes| mod_run["Moderation step:<br/>extract (input,output)<br/>dedup<br/>BERT classify_batch<br/>cache"]
  skip_mod --> compute
  mod_run --> compute["for each kept span:<br/>build_context reads ALL 3 caches<br/>compute pii_detected, prompt_injection_detected, toxicity_detected"]
  compute --> emit["emit rows + metric_meta<br/>(score, model_ran, types, violations, location)"]
```

### Three-level efficiency

1. **CH-side prefilter** — `span_types=('model_call',)` ensures non-LLM spans never load into memory.
2. **Stage 1 gate** — drops spans for which no Safety toggle is active. If a customer has only PII enabled on solution X, only X's `model_call` spans reach the ML stack.
3. **PrefillStep sub-filtering** — each step gets only the spans whose threshold metric matches `step.metrics`. A pod that never sees an active `toxicity_detected` toggle never loads the moderation BERT.
4. **Content-hash dedup** — within and across batches, the same prompt is analyzed once. LRU cache (size `COMPASS_PII_CACHE_MAX` / `COMPASS_TOXICITY_CACHE_MAX`); hit rate is 95%+ on established workloads (system prompts repeat across thousands of spans).

## 4. Lazy model loading

```mermaid
flowchart TD
  pod_start["pod start"] --> ready["readiness probe → True"]
  ready --> first_batch["first batch"]
  first_batch --> step_check{"step.analyze called<br/>with non-empty input?"}
  step_check -->|no| no_load["model file stays on disk<br/>~1.5 GB / model not in RAM"]
  step_check -->|yes & first time| load["from deidentifier import PresidioEngine<br/>(or toxicity_observability.PI / Moderation)<br/>load weights from /opt/models/<br/>~3-30 s pause for first span"]
  load --> ready_step["module-level singleton stays loaded<br/>for pod lifetime"]
  ready_step --> serve
  no_load --> serve["serve cached results / no-op"]
```

A Safety pod whose customer has only PII enabled never loads the toxicity models — saves ~1 GB of RAM.

## 5. Model artifacts

Baked into the `compass-worker:safety` image (~5.5 GB) at `/opt/models/`:

| Model | Source | Size |
|---|---|---|
| Presidio NER (spaCy) | `en_core_web_sm` baked in | ~220 MB |
| Prompt Injection BERT | HF snapshot, ONNX | ~500 MB |
| Moderation BERT | HF snapshot, ONNX | ~500 MB |

**Why bake into image** (vs init container + PVC): zero model-fetch on pod boot, image SHA pins weights to a reproducible deploy, no HF outage risk at runtime. Cost: 5.5 GB image pulled once per node, amortized across pod restarts.

## 6. Topology + scaling

```mermaid
flowchart LR
  ss["StatefulSet compass-worker-safety<br/>replicas=N (1..16)"] --> pod0["safety-0<br/>WORKER_PARTITION_INDEX=0"]
  ss --> pod1["safety-1<br/>WORKER_PARTITION_INDEX=1"]
  ss --> podN["safety-(N-1)"]
  pod0 --> slot_set0["compute_slots(0, N, 16)"]
  pod1 --> slot_set1["compute_slots(1, N, 16)"]
  podN --> slot_setN["..."]
  slot_set0 -->|"partition_id IN owned"| ch[(compass_raw_spans)]
  slot_set1 -->|"partition_id IN owned"| ch
  slot_setN --> ch
  pod0 -->|"UPSERT lens='safety', partition_key='slot:N'"| wc[(worker_checkpoints)]
```

| Pods | Slots per pod | When |
|---|---|---|
| 1 | 16 | low traffic / dev |
| 2 | 8 | early prod |
| 4 | 4 | moderate prod |
| 8 | 2 | high prod |
| 16 | 1 | max parallelism (1 slot per pod) |

### Slot derivation

`compass_raw_spans.partition_id = cityHash64(trace_id) % 16` is materialized at ingest. Set skip index makes per-slot fetches scan only relevant data parts. Each pod's `compute_slots(idx, count, total=16)` returns its owned subset.

### Scale up / down

```bash
# Atomically patches replicas AND WORKER_PARTITION_COUNT, watches rollout
./infra/k8s/scale-safety.ps1 -Replicas 8
```

Slot checkpoints persist across changes — a new pod inheriting slot 7 reads slot 7's row from `worker_checkpoints` and resumes. **No manual rebalancing.**

| Resources | Request | Limit |
|---|---|---|
| CPU | 1000m | 2000m |
| Memory | 4Gi | 6Gi (incl. ~1.5 GB model + LRU caches) |

## 7. Caching

| Cache | Size | TTL | Eviction |
|---|---|---|---|
| ToggleCache | small | 300s | TTL refresh |
| PII results LRU | `COMPASS_PII_CACHE_MAX` (~10k) | none | LRU |
| PI results LRU | `COMPASS_TOXICITY_CACHE_MAX` (~10k) | none | LRU |
| Moderation results LRU | `COMPASS_TOXICITY_CACHE_MAX` (~10k) | none | LRU |
| Model weights | `/opt/models/` | pod lifetime | none |

Content hash = `sha256(text)[:16]` — short enough for dict keys, long enough that collisions don't matter.

## 8. Observability

Standard worker metrics + watch:

| Metric | Watch for |
|---|---|
| `compass_worker_checkpoint_lag_seconds{lens="safety"}` | Climbing → scale up |
| `compass_worker_batch_duration_seconds{lens="safety"}` | p95 > poll_sec → scale up |
| `compass_worker_batches_total{lens="safety", result="error"}` | Persistent errors → check model load, OOM |

HPA driver (when ready):

```yaml
metrics:
- type: Pods
  pods:
    metric: { name: compass_worker_checkpoint_lag_seconds }
    target: { type: AverageValue, averageValue: "60" }
```

Target = "keep average lag under 60s across all Safety pods".

## 9. Failure modes

| Failure | Outcome |
|---|---|
| Model file corrupt / missing | `_verify_models_ready` at construction raises clear error; pod CrashLoopBackOff. Roll back image |
| OOM during ML inference | Pod killed, restarted. If recurring → increase memory limit or scale up (fewer spans per pod) |
| Tokenizer fails on weird input | One span errors → whole batch rolls back → next batch retries forever. **Fix forward** — patch the lens to catch and emit "scoring_failed" meta |
| Two Safety pods owning same slot (manual `kubectl scale` desync) | Both fetch same spans, both try to write. Dedup tokens (`safety:s{slot}:{ts}:{n}` are identical) make CH drop one — correctness preserved, compute wasted |
| First batch after pod restart is slow | Models cold-load on first non-empty input. 3–30s pause. Readiness probe stays True throughout, but checkpoint lag spikes |

## 10. Adding a Safety metric

Adding a new prefill step (new model):

1. Implement the analyzer (mirroring `deidentifier` / `toxicity_observability` package shape).
2. Add a `PrefillStep` in `SafetyWorker.__init__`.
3. Declare specs with the appropriate metric names; add to the relevant METRICS set so sub-filtering works.
4. Cache + lazy load follow automatically.

Adding a metric over an existing step:

1. Declare spec in `SPECS`.
2. Add metric name to the right METRICS set (`PII_METRICS` / `PROMPT_INJECTION_METRICS` / `MODERATION_METRICS`).
3. Have `build_context` map cached step output to the new metric.

In both cases: redeploy reconciler first (so metric_catalog + thresholds seed), then redeploy safety.
