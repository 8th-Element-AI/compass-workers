# Signal Workers

Production observability workers for the Signal platform. Each lens runs as
an independent Kubernetes Deployment, reads immutable spans from ClickHouse,
computes its metrics, and writes the results back. State (per-lens
high-watermark) lives in Postgres; writes are idempotent under restart.

```
signal_raw_spans  ──►  [ lens worker ]  ──►  signal_derived_metrics  ──(MV)──►  signal_aggregated_metrics
   (what happened)        (compute)              (one row / metric / span)         (1-min rollup buckets)
                                ▲
                                │
                            worker_checkpoints (PG)   ─── one high-watermark per lens
```

**One worker per lens, one image per lens, one Deployment per lens.** Performance and Cost are CPU-only and tiny. Safety carries the ML stack (Presidio, transformers, fasttext) and the model weights.

> For *why* it's built this way — the engine vs lens split, the aggregation
> internals, scoping conventions, design tradeoffs — see **ARCHITECTURE.md**.

---

## Table of contents

1. [What's in this repo](#1-whats-in-this-repo)
2. [Lenses](#2-lenses)
3. [Prerequisites](#3-prerequisites)
4. [Quick start (local dev)](#4-quick-start-local-dev)
5. [Docker images](#5-docker-images)
6. [Production deployment (Kubernetes)](#6-production-deployment-kubernetes)
7. [Configuration reference](#7-configuration-reference)
8. [Operations](#8-operations)
9. [Data correctness guarantees](#9-data-correctness-guarantees)
10. [Troubleshooting](#10-troubleshooting)
11. [Development](#11-development)

---

## 1. What's in this repo

```
signal-workers/
├── run_worker.py            CLI entrypoint — launches ONE lens per invocation
├── show_specs.py            print a lens's metric registry as a table
├── validate_performance.py  offline correctness check vs the mock derived data
├── migrate_checkpoints.py   one-shot: copy file checkpoints into worker_checkpoints PG table
├── Dockerfile               builds signal-worker:base (framework only)
├── Dockerfile.performance   builds signal-worker:performance
├── Dockerfile.cost          builds signal-worker:cost
├── Dockerfile.safety        builds signal-worker:safety (extends :base with ML stack)
├── .env.example             template for local dev (host mode)
└── signal_worker/
    ├── config.py            env-driven Pydantic Settings (CH/PG/run-loop/toxicity/PII)
    ├── base.py              engine: fetch → compute → write → checkpoint
    ├── spec.py              MetricSpec + SpecWorker (spec-driven engine + PrefillStep pipeline)
    ├── checkpoint.py        Postgres-backed checkpoint store
    ├── observability.py     /healthz, /readyz, /metrics HTTP server + Prometheus counters
    ├── patterns.py          reusable compute patterns (column_latency, ratio, ctx_value, …)
    ├── predicates.py        reusable "which spans does this apply to" filters
    ├── pricing.py           PricingCache — Cost lens reads from Postgres components.pricing
    ├── toggle_cache.py      Stage-1 gate: skip spans with no active threshold
    ├── utils.py             LRUCache, helpers
    └── lenses/
        ├── performance.py   Performance lens — 14 specs
        ├── cost.py          Cost lens — 22 specs
        └── safety.py        Safety lens — 4 specs (PII + toxicity + prompt injection + moderation)
```

Sibling repos this depends on:

- `toxicity/` — toxicity, prompt-injection, and moderation classifiers (fasttext + DeBERTa). Required only by Safety.
- `PII/` — Presidio-backed PII detection. Required only by Safety.

Both are installed as editable packages inside the Safety image and not at all inside `:base` / `:performance` / `:cost`.

---

## 2. Lenses

### Performance — 14 specs

Reads from `span_type` and `metadata.*`. CPU-only. Suitable for any infra.

Sub-categories:

- **Latency**: `latency`, `time_to_first_token`, `queue_wait_time`, `scheduling_delay`.
- **Throughput** (read-time, not emitted): `throughput`, `concurrency`.
- **Errors**: `error_rate`, `timeout_count`.
- **Retry / resilience**: `retry_count`, `retry_delay`, `rate_limit_hit`, `rate_limit_wait`.
- **Batch / records**: `records_processed`, `token_throughput`.

Three metrics are **read-time** gauges/rates (`throughput`, `concurrency`, `messages_in_flight`) — declared with `aggregation_derived()` and not emitted per span.

### Cost — 22 specs

Reads from `span_type IN (model_call, embedding, tool_call, retrieval)` and pulls component pricing from Postgres on a TTL cache. CPU-only.

Sub-categories:

- **Token costs**: `input_tokens_cost`, `output_tokens_cost`, `total_tokens_cost`, `cached_tokens_cost`.
- **Monetary**: `monetary_cost`, `monetary_input_cost`, `monetary_output_cost`.
- **Tool / embedding / retrieval costs**: `tool_api_cost`, `embedding_cost`.
- **Waste & efficiency**: `wasted_cost`, `retry_cost`, `cache_savings`, `cost_per_record`, `cost_per_outcome`.
- **Budget tracking**: `budget_utilization`, `burn_rate`.

`span_types = ["model_call", "embedding", "tool_call", "retrieval"]` is pushed into `fetch_batch`'s SQL so the worker never reads spans it can't bill.

### Safety — 4 specs

The expensive lens. Loads ML models lazily and runs batched inference.

| Spec | Pattern | Models involved |
|---|---|---|
| `pii_count` | Presidio NER over input + output | `gravitee-io/bert-small-pii-detection` |
| `pii_detected` | `pii_count > 0` | (same) |
| `prompt_injection_detected` | FastText router → DeBERTa PI head | `Krishagarwal314/safety-fasttext-router`, `Krishagarwal314/safety-prompt-injection-onnx-int8` |
| `toxicity_detected` | FastText router → DeBERTa moderation head | `Krishagarwal314/safety-fasttext-router`, `Krishagarwal314/safety-moderation` |

Pipeline per batch:

1. **Stage 1 gate** — drop spans with no active safety threshold.
2. **FastText router** — single forward pass over all unique input+output texts. Handles ~99% of traffic cheaply.
3. **DeBERTa PI head** — only on texts the router escalates as possibly attack.
4. **DeBERTa moderation head** — only on texts the router escalates as possibly harmful/sexual.
5. **Presidio NER** — runs in a threadpool across batch_size concurrent texts.

Models load **lazily on first use**, so a pod with only `pii_detected` toggles active never loads the toxicity models.

---

## 3. Prerequisites

### Software

| Component | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Worker runtime |
| Docker | with BuildKit | Image builds (`DOCKER_BUILDKIT=1`) |
| ClickHouse | 24.x+ | Span store + derived/aggregated metrics |
| Postgres | 14+ | Registry, bindings, thresholds, **worker_checkpoints** |
| HuggingFace account | with a token | One-time, build-time only, for Safety image |

### Infrastructure schemas

The workers expect these tables to exist:

**ClickHouse** (`infra/clickhouse/init/00_schema.sql`):
- `signal_raw_spans` (MergeTree) — read-only for workers.
- `signal_derived_metrics` (MergeTree, with `non_replicated_deduplication_window = 1000`) — workers write here.
- `signal_aggregated_metrics` (AggregatingMergeTree) — populated by the `mv_agg_base` materialized view, **never written to by workers**.

**Postgres**:
- `solutions`, `endpoints`, `workflows`, `agents`, `components`, `bindings` — entity registry.
- `performance_thresholds`, `quality_thresholds`, `cost_thresholds`, `safety_thresholds`, `outcomes_thresholds` — toggle gates (read by `ToggleCache`).
- `components.pricing` JSONB — read by Cost's `PricingCache`.
- **`worker_checkpoints`** (`infra/postgres/init/03_worker_checkpoints.sql`) — high-watermark per lens.

Run the infra Docker compose stack to get all of the above:

```powershell
cd infra
docker compose up -d
```

---

## 4. Quick start (local dev)

For host-mode development against a local infra stack.

### 4.1 Install Python deps

From the repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

The `-e ./PII` and `-e ./toxicity` references in `requirements.txt` install the sibling packages editable.

### 4.2 Configure

```powershell
cd signal-workers
copy .env.example .env
notepad .env
```

Minimum required vars (defaults work for the `infra/` compose stack):

```bash
CH_HOST=localhost
CH_PORT=8123
CH_DB=signal

PG_DSN=postgresql://signal:signal@localhost:5432/signal

# Safety lens — required if you'll run safety locally
HF_TOKEN=hf_...
SIGNAL_TOXICITY_MODELS_ROOT=E:/8thelement/Signal/toxicity/models
```

See [S7 Configuration reference](#7-configuration-reference) for the full list.

### 4.3 Pre-download Safety models (one-time, if running safety on host)

```powershell
cd ..\toxicity
toxicity-observe download --config configs/runtime.yaml
python -c "from transformers import pipeline; pipeline('ner', model='gravitee-io/bert-small-pii-detection', aggregation_strategy='first')"
cd ..\signal-workers
```

These are baked into the Docker `:safety` image automatically, so this step is only for host-mode dev runs.

### 4.4 Run a lens

```powershell
python run_worker.py --worker performance --once
python run_worker.py --worker cost --once
python run_worker.py --worker safety --once

# Continuous (production-like):
python run_worker.py --worker performance

# Show the metric registry without connecting to anything:
python run_worker.py --worker performance --specs
```

### 4.5 Verify in ClickHouse

```sql
-- per-metric row counts the worker just wrote
SELECT metric, count(), max(ts) FROM signal_derived_metrics
GROUP BY metric ORDER BY metric;

-- rolled-up p95 latency for the last hour
SELECT
  quantilesTDigestMerge(0.95)(quantiles) AS p95
FROM signal_aggregated_metrics
WHERE metric = 'latency' AND ts > now() - INTERVAL 1 HOUR;
```

---

## 5. Docker images

### 5.1 Image hierarchy

```
                        python:3.11-slim
                               │
                               ▼
                  signal-worker:base  (~400 MB, internal)
                    │           │           │
        ┌───────────┘           │           └───────────┐
        ▼                       ▼                       ▼
 :performance              :cost                     :safety
 (~400 MB)                 (~400 MB)                 (~5.5 GB, + ML stack + model weights)
```

`:base` is the framework: Python venv, clickhouse-connect, psycopg, pydantic-settings, the `signal_worker/` package, and `run_worker.py`. It is **not deployed directly**; only the three child images are.

`:performance` and `:cost` are `:base` + a different `ENTRYPOINT`. Identical byte content otherwise — the only difference is the `--worker <name>` argument baked in.

`:safety` extends `:base` with the heavy stack: torch CPU, transformers, presidio-analyzer, spaCy + en_core_web_sm, the editable `toxicity/` and `PII/` packages, the model weights at `/opt/models`, and the HuggingFace cache at `/opt/hf-cache`.

### 5.2 Build order

`:base` must exist before the child images can `FROM` it.

```powershell
cd E:\8thelement\Signal     # repo root, NOT signal-workers/
$env:DOCKER_BUILDKIT = "1"
$env:HF_TOKEN = "hf_..."     # only needed for :safety

# 1. base
docker build -f signal-workers/Dockerfile -t signal-worker:base .

# 2. performance + cost (~5 seconds each)
docker build -f signal-workers/Dockerfile.performance -t signal-worker:performance .
docker build -f signal-workers/Dockerfile.cost        -t signal-worker:cost .

# 3. safety (~5 min cold, ~30s warm if model layers are cached)
docker build --secret id=hf_token,env=HF_TOKEN `
    -f signal-workers/Dockerfile.safety -t signal-worker:safety .
```

There's a convenience script at `signal-workers/build-all.ps1` that does all four in order.

### 5.3 Image internals

| Path | What's there |
|---|---|
| `/opt/venv` | The Python venv (all PyPI deps + editable installs) |
| `/opt/signal/PII` | Editable install of the PII package (Safety only) |
| `/opt/signal/toxicity` | Editable install of the toxicity package (Safety only) |
| `/opt/models/fasttext` | FastText router weights |
| `/opt/models/transformers/prompt_injection` | DeBERTa PI head |
| `/opt/models/transformers/moderation` | DeBERTa moderation head |
| `/opt/models/onnx_int8/prompt_injection` | ONNX int8 PI head (CPU optimized) |
| `/opt/hf-cache` | HuggingFace cache containing the Presidio NER model |
| `/app` | `signal_worker/` package + `run_worker.py` |

### 5.4 Running locally against your infra

Each image has `ENTRYPOINT ["python", "run_worker.py", "--worker", "<lens>"]` baked in, so `docker run` extras are appended as args.

Inside the container, `localhost` is the container itself — use `host.docker.internal` (Docker Desktop) for the host's ClickHouse and Postgres. Maintain a separate `signal-workers/.env.docker` with the host overrides:

```bash
# .env.docker — same as .env except:
CH_HOST=host.docker.internal
PG_DSN=postgresql://signal:signal@host.docker.internal:5432/signal
```

> **Inline comments in `.env.docker` will break it.** Docker's `--env-file` parser does NOT strip inline comments. Put comments on their own lines.

Then run:

```powershell
docker run --rm `
  --env-file signal-workers\.env.docker `
  -p 8080:8080 `
  signal-worker:performance --once

docker run --rm `
  --env-file signal-workers\.env.docker `
  -p 8080:8080 `
  signal-worker:cost --once

docker run --rm `
  --env-file signal-workers\.env.docker `
  -p 8080:8080 `
  signal-worker:safety --once
```

`-p 8080:8080` exposes the observability port so you can `curl http://localhost:8080/metrics` while the worker runs.

The `signal-workers/run-worker.ps1` helper wraps the long invocation:

```powershell
.\signal-workers\run-worker.ps1 -Worker performance -Once
```

---

## 6. Production deployment (Kubernetes)

### 6.1 Topology

Three Deployments, three image refs, one replica each:

```
Namespace: signal
  ConfigMap  signal-worker-config      (non-secret env)
  Secret     signal-worker-secrets     (PG_DSN, CH_PASSWORD)
  Deployment signal-worker-performance image=signal-worker:performance, replicas=1
  Deployment signal-worker-cost        image=signal-worker:cost,        replicas=1
  Deployment signal-worker-safety      image=signal-worker:safety,      replicas=1
  Service    signal-worker-metrics     selector=app=signal-worker, port=8080
```

Each Deployment specifies:

- **Liveness probe**: `GET /healthz` on port 8080 (returns 200 if the process is alive).
- **Readiness probe**: `GET /readyz` on port 8080 (returns 200 once the poll loop has started).
- **`terminationGracePeriodSeconds: 60`** — long enough for an in-flight batch to write + checkpoint cleanly on SIGTERM.
- **Resource requests/limits** — modest for Performance/Cost, generous CPU + 4Gi memory for Safety.

The Service is purely for Prometheus scraping discovery. Workers don't serve HTTP traffic to other services.

### 6.2 Pushing images to a registry

For GCP Artifact Registry:

```powershell
# Tag for the registry
docker tag signal-worker:base        REGION-docker.pkg.dev/PROJECT/signal/signal-worker:base
docker tag signal-worker:performance REGION-docker.pkg.dev/PROJECT/signal/signal-worker:performance
docker tag signal-worker:cost        REGION-docker.pkg.dev/PROJECT/signal/signal-worker:cost
docker tag signal-worker:safety      REGION-docker.pkg.dev/PROJECT/signal/signal-worker:safety

# Authenticate
gcloud auth configure-docker REGION-docker.pkg.dev

# Push
docker push REGION-docker.pkg.dev/PROJECT/signal/signal-worker:base
docker push REGION-docker.pkg.dev/PROJECT/signal/signal-worker:performance
docker push REGION-docker.pkg.dev/PROJECT/signal/signal-worker:cost
docker push REGION-docker.pkg.dev/PROJECT/signal/signal-worker:safety
```

Adjust REGION (e.g. `us-central1`) and PROJECT to match your environment.

### 6.3 Applying manifests

K8s manifests live in `infra/k8s/`. Once written:

```bash
kubectl apply -f infra/k8s/namespace.yaml
kubectl apply -f infra/k8s/configmap.yaml
kubectl apply -f infra/k8s/secret.yaml          # or use a secrets manager
kubectl apply -f infra/k8s/deployment-performance.yaml
kubectl apply -f infra/k8s/deployment-cost.yaml
kubectl apply -f infra/k8s/deployment-safety.yaml
kubectl apply -f infra/k8s/service.yaml
```

Verify:

```bash
kubectl -n signal get pods
kubectl -n signal logs deploy/signal-worker-performance --tail 50
kubectl -n signal port-forward svc/signal-worker-metrics 8080:8080
curl http://localhost:8080/metrics | grep signal_worker
```

### 6.4 First-time deployment checklist

1. ClickHouse schema applied, including `non_replicated_deduplication_window = 1000` on `signal_derived_metrics`.
2. Postgres schema applied, including `worker_checkpoints` table.
3. If migrating from a previous file-based deployment, run `migrate_checkpoints.py` against PG before pods start (so they don't replay from epoch).
4. Images pushed to your registry.
5. Secret created with valid `PG_DSN` and `CH_PASSWORD`.
6. HuggingFace token NOT in the secret — it's build-time only.
7. ConfigMap populated with `CH_HOST`, `CH_PORT`, `CH_DB`, `WORKER_BATCH`, `WORKER_POLL_SEC`, etc.

### 6.5 Scaling considerations

**Today: 1 replica per lens.** Multi-replica of the same lens would cause both pods to fetch the same spans and double-process them. The dedup tokens prevent double-counts in ClickHouse, but the wasted compute is still real.

**Scaling Safety later** requires partitioned consumption — each replica owns a hash-partition of `trace_id`. Designed-for in the schema (`worker_checkpoints.partition_key` is already a column, hardcoded `'default'` for now). Implementation deferred until throughput demands it.

---

## 7. Configuration reference

All configuration is environment-driven via `signal_worker/config.py` (Pydantic Settings). Local-friendly defaults; production overrides via ConfigMap + Secret.

### 7.1 Connection settings

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `CH_HOST` | `localhost` | all | ClickHouse host |
| `CH_PORT` | `8123` | all | ClickHouse HTTP port |
| `CH_DB` | `signal` | all | ClickHouse database |
| `CH_USER` | `default` | all | ClickHouse user |
| `CH_PASSWORD` | `` | all | ClickHouse password |
| `PG_DSN` | `postgresql://postgres@localhost:5432/signal` | all (toggles, checkpoints; Cost reads pricing) | Postgres DSN |

### 7.2 Worker run loop

| Variable | Default | Purpose |
|---|---|---|
| `WORKER_BATCH` | `5000` | Max spans fetched per batch |
| `WORKER_POLL_SEC` | `2.0` | Sleep between empty polls (continuous mode) |
| `SIGNAL_TOGGLE_TTL` | `300` | Seconds between PG refreshes of the toggle cache |

### 7.3 Safety — Toxicity / Prompt Injection / Moderation

| Variable | Default | Purpose |
|---|---|---|
| `SIGNAL_TOXICITY_MODELS_ROOT` | `/opt/models` (in container) | Base path for the four model artifacts |
| `SIGNAL_TOXICITY_FASTTEXT_PATH` | `fasttext/router_head.ftz` | Relative to MODELS_ROOT |
| `SIGNAL_TOXICITY_PI_PATH` | `transformers/prompt_injection` | Relative to MODELS_ROOT |
| `SIGNAL_TOXICITY_PI_ONNX_PATH` | `onnx_int8/prompt_injection` | Relative to MODELS_ROOT |
| `SIGNAL_TOXICITY_MOD_PATH` | `transformers/moderation` | Relative to MODELS_ROOT |
| `SIGNAL_TOXICITY_DEVICE` | `cpu` | `cpu` or `cuda` |
| `SIGNAL_TOXICITY_MAX_LENGTH` | `128` | Token max for transformers |
| `SIGNAL_TOXICITY_FP16` | `true` | Use fp16 when device=cuda |
| `SIGNAL_TOXICITY_BATCH_SIZE` | `32` | Texts per transformer forward pass |
| `SIGNAL_TOXICITY_ATTACK_ROUTE` | `0.05` | FastText threshold to escalate to PI BERT |
| `SIGNAL_TOXICITY_MODERATION_ROUTE` | `0.05` | FastText threshold to escalate to Moderation BERT |
| `SIGNAL_TOXICITY_FAST_ALLOW` | `0.02` | FastText threshold to short-circuit as safe |
| `SIGNAL_TOXICITY_FASTTEXT_DIRECT` | `0.97` | Above this, trust FastText and skip BERT |
| `SIGNAL_TOXICITY_PI_REVIEW` | `0.5` | PI score threshold for `prompt_injection_detected` |
| `SIGNAL_TOXICITY_HARMFUL_REVIEW` | `0.5` | Moderation harmful score threshold |
| `SIGNAL_TOXICITY_SEXUAL_REVIEW` | `0.5` | Moderation sexual score threshold |
| `SIGNAL_TOXICITY_CACHE_MAX` | `20000` | Per-worker content-hash result cache |

### 7.4 Safety — PII (Presidio)

| Variable | Default | Purpose |
|---|---|---|
| `SIGNAL_PII_NER_MODEL` | `gravitee-io/bert-small-pii-detection` | HuggingFace model id (pre-cached in image) |
| `SIGNAL_PII_BATCH` | `4` | ThreadPoolExecutor width for `analyze_batch` |
| `SIGNAL_PII_CACHE_MAX` | `20000` | Per-worker content cache |

### 7.5 Observability

| Variable | Default | Purpose |
|---|---|---|
| `OBSERVABILITY_PORT` | `8080` | Port for `/healthz`, `/readyz`, `/metrics` |
| `HOSTNAME` | (from container) | Recorded as `updated_by` in `worker_checkpoints` |

### 7.6 Build-time only

| Variable | Used by | Purpose |
|---|---|---|
| `HF_TOKEN` | `Dockerfile.safety` (BuildKit secret) | Auth for private HuggingFace model repos at image build |

Never set `HF_TOKEN` at runtime. It does not appear in any image layer.

---

## 8. Operations

### 8.1 Health checks

Each worker pod exposes three HTTP endpoints on `OBSERVABILITY_PORT` (default 8080):

| Endpoint | Returns | Used by |
|---|---|---|
| `/healthz` | 200 if the process is alive | K8s liveness probe |
| `/readyz` | 200 once the poll loop has started, else 503 | K8s readiness probe; gates traffic during rolling updates |
| `/metrics` | Prometheus exposition format | Prometheus scrape |

```bash
curl http://localhost:8080/healthz
# ok

curl http://localhost:8080/readyz
# ready  (or "not ready" with 503 during startup)

curl http://localhost:8080/metrics | head -40
# # HELP signal_worker_batches_total Total worker batches processed.
# ...
```

### 8.2 Prometheus metrics

All metrics are labeled by `lens`:

| Metric | Type | Meaning |
|---|---|---|
| `signal_worker_batches_total{lens,result}` | counter | result ∈ `success | error | empty` |
| `signal_worker_spans_processed_total{lens}` | counter | Spans fetched from ClickHouse |
| `signal_worker_rows_emitted_total{lens}` | counter | Derived rows written |
| `signal_worker_skipped_at_gate_total{lens}` | counter | Spans dropped at Stage 1 |
| `signal_worker_batch_duration_seconds{lens}` | histogram | `process_batch()` wall-clock |
| `signal_worker_write_duration_seconds{lens}` | histogram | ClickHouse insert wall-clock |
| `signal_worker_checkpoint_lag_seconds{lens}` | gauge | now − last span's `recorded_at` |

Useful queries:

```promql
# How fast is each lens keeping up with spans?
rate(signal_worker_spans_processed_total[5m])

# Safety lag — alert if it climbs above 5 minutes
signal_worker_checkpoint_lag_seconds{lens="safety"} > 300

# p95 batch latency per lens
histogram_quantile(0.95, rate(signal_worker_batch_duration_seconds_bucket[5m]))

# Worker crash rate
rate(signal_worker_batches_total{result="error"}[5m]) > 0

# Effectiveness of the Stage 1 gate
rate(signal_worker_skipped_at_gate_total[5m])
  / rate(signal_worker_spans_processed_total[5m])
```

### 8.3 Logs

stdout-only, structured-ish. The notable log lines per batch (Performance lens example):

```
Fetched 3160 spans newer than 2026-06-14 09:14:23.117

[performance] batch=3160 processed=2890 skipped_at_gate=270 emitted=18420
[performance] 3160 spans -> 18420 metrics (wm=2026-06-14 09:18:51.842 token=performance:2026-06-14 09:18:51.842:3160)
```

Safety adds per-step timing:

```
[safety:pii] analyzing 384 unique texts from 412 spans
[safety:router] FastText over 412 unique texts
[safety:pi] FastText cleared all 412 texts; PI BERT not loaded
[safety:mod] Moderation BERT over 12 of 412 routed texts
[safety] latency | total=2840.1ms | pii=2410.0ms (384 texts) | toxicity_router=180.0ms (412 texts) | prompt_injection=0.0ms (0 texts) | moderation=240.0ms (12 texts) | emit=10.1ms | rows=824
```

### 8.4 Graceful shutdown

SIGTERM → `BaseWorker.stop()` sets a threading event → the poll loop exits at the next batch boundary. Any in-flight batch completes its `write` and `save_checkpoint` before the process exits.

K8s gives `terminationGracePeriodSeconds: 60` for this drain. If a batch is still running past 60s, K8s sends SIGKILL; on restart, the dedup token guarantees no double-write.

### 8.5 Restart and resume

Pods are stateless. On restart:

1. `load_checkpoint()` reads the saved watermark from `worker_checkpoints` in Postgres.
2. `fetch_batch(since=watermark, ...)` returns spans newer than that.
3. Compute → write → save_checkpoint as normal.

If the prior shutdown happened mid-batch:
- If between fetch and write: nothing was written; re-fetch is harmless.
- If between write and save_checkpoint: re-fetch returns the same spans, write recomputes the same dedup token, ClickHouse drops the duplicate insert and the MV doesn't fire. No double-count.

### 8.6 Routine ops tasks

**Rewind a lens's checkpoint** (e.g. to reprocess a window):

```sql
UPDATE worker_checkpoints
SET watermark = '2026-06-14 00:00:00.000', updated_by = 'manual-rewind'
WHERE lens = 'safety';
```

The next poll cycle re-fetches from there. The dedup window protects against doubling the source side; the aggregated table can be cleaned by reprocessing into a different MV if the rewind is large.

**Promote a new image version** (rolling restart):

```bash
kubectl -n signal set image deploy/signal-worker-safety worker=REGION-docker.pkg.dev/PROJECT/signal/signal-worker:safety-v1.2
```

K8s rolls one pod at a time. New pod starts, becomes ready, old pod gets SIGTERM, drains its batch, exits.

**Check what each lens last did**:

```sql
SELECT lens, watermark, updated_at, updated_by
FROM worker_checkpoints
ORDER BY lens;
```

---

## 9. Data correctness guarantees

### 9.1 Append-only writes

Workers only insert into `signal_derived_metrics`. They never UPDATE, DELETE, or touch `signal_aggregated_metrics`. The materialized view does the aggregation automatically.

### 9.2 Idempotency via dedup tokens

Each batch is written with `insert_deduplication_token = "<lens>:<newest_recorded_at>:<batch_size>"`. ClickHouse remembers the last 1000 tokens per table (`non_replicated_deduplication_window = 1000`). A retried write with a matching token is dropped silently and **the MV does not fire** — so the rollup table also stays consistent.

This means:

- ✅ **Crash between write and save_checkpoint**: safe. Re-fetch produces the same token; CH drops the insert.
- ✅ **Pod restart mid-loop**: safe. Same mechanism.
- ✅ **Manual replay** (e.g. rewinding a checkpoint to backfill a fix): partially safe. The dedup window covers ~the last 1000 batches' tokens. If you rewind further back than that, expect duplicates in the source table; query with `FINAL` or aggregate-with-dedup.

### 9.3 Per-lens isolation

Each lens has its own checkpoint row in `worker_checkpoints`. A crash, lag, or rewind in Safety does not affect Performance or Cost. Each lens also has its own image, ConfigMap-shared env, and Deployment.

### 9.4 Known caveats

- **Span-boundary tie**: `recorded_at > wm LIMIT N` can skip rows that share the boundary timestamp if the tie spans a batch edge. Mitigated by `WORKER_BATCH = 5000`; a true fix is `(recorded_at, span_id)` cursor pagination, deferred.
- **No exactly-once across CH and PG**: the dedup token gives effective once-per-row in ClickHouse, but checkpoint advancement and CH writes are two operations. The token closes the only practical window where this matters.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Fetched 0 spans newer than 1970-01-01` repeatedly | New lens with no checkpoint row in PG | Expected on first run; will backfill from epoch. Migrate file checkpoints if you have any: `python migrate_checkpoints.py --state-dir ../state-dir` |
| `Connection refused` to `host.docker.internal:8123` | Running container against host CH but `CH_HOST=localhost` in `.env.docker` | Set `CH_HOST=host.docker.internal` (Docker Desktop) or use the actual host IP |
| Pydantic ValidationError parsing int env var | Inline comments in `.env.docker` (Docker doesn't strip them like python-dotenv does) | Move comments to their own lines; never on the same line as a value |
| `RuntimeError: Model 'gravitee-io/...' is not cached locally` | PII NER model wasn't pre-cached in the Safety image | Confirm the `python -c "from transformers import pipeline; pipeline('ner', ...)"` step is in `Dockerfile.safety` Stage 1 |
| Image is 9+ GB | `.dockerignore` not being read at build context root | `.dockerignore` must be at repo root (`E:\8thelement\Signal\.dockerignore`), not inside `signal-workers/` |
| `numpy._core._exceptions._ArrayMemoryError` from fasttext | NumPy 2.x incompat with old `fasttext-wheel` | Use `fasttext-numpy2-wheel` (already in toxicity deps); on Windows host you may need a manual patch — see toxicity README |
| `[performance] rows=0` despite spans being present | No active threshold rows in Postgres for the entity path the spans belong to | Insert at least one row in `performance_thresholds` (or whichever lens's table) matching the span's solution_id / scope |
| Pod stuck in `Pending` | Insufficient resources on the node | Check `kubectl describe pod` — Safety needs ~4Gi memory and ~1 vCPU |
| `/readyz` returns 503 forever | `run_poll` failed to start (look for stack trace in logs) | Common cause: PG DSN wrong, CH unreachable; check `kubectl logs` |
| Metrics in `signal_aggregated_metrics` look 2× expected | Pre-Phase 4.2 deployment had a crash that double-wrote, or the ALTER for `non_replicated_deduplication_window` wasn't applied | Verify with `SHOW CREATE TABLE signal_derived_metrics` — settings clause must include `non_replicated_deduplication_window = 1000` |

---

## 11. Development

### 11.1 Adding a metric

If a compatible pattern and predicate already exist in `patterns.py` / `predicates.py`, a new metric is one line in a lens's `SPECS` list:

```python
_spec("my_new_metric", llm_call, metadata_numeric("my_field"),
      ["metadata.my_field"], unit="count", window="5m")
```

Then run `python run_worker.py --worker <lens> --specs` to confirm it shows in the registry.

### 11.2 Adding a lens

1. Create `signal_worker/lenses/<lens>.py` with a `SpecWorker` subclass:
   - Set `lens` (string), a `SPECS` list of `MetricSpec`, optionally `span_types`.
   - Implement `build_context(span)` to parse everything the patterns need, once.
2. Register it in `run_worker.py`'s `LENSES` dict.
3. Add a `Dockerfile.<lens>` if it needs its own image:
   ```dockerfile
   FROM signal-worker:base
   ENTRYPOINT ["python", "run_worker.py", "--worker", "<lens>"]
   ```
4. Add a row to `infra/postgres/init/02_thresholds.sql` if it needs its own toggle table.
5. Validate offline with a CSV first, then run `--once` live.

See **ARCHITECTURE.md → "Adding a lens"** for the full walkthrough.

### 11.3 Running tests

```powershell
cd signal-workers
python validate_performance.py
```

That runs the Performance lens against an exported spans CSV and diffs against the expected derived rows.

### 11.4 Local + container parity

The `--csv` path in `run_worker.py` runs the exact same `compute()` logic without needing ClickHouse or Postgres. Use it to validate logic changes before they touch live infra.

```powershell
python run_worker.py --worker performance --csv ../infra/data/signal_raw_spans.csv --out perf-out.csv
```

---

## Appendix — Related docs

- **ARCHITECTURE.md** — engine design, scoping conventions, MV details, scaling tradeoffs.
- **infra/README.md** — ClickHouse + Postgres compose stack, schema files, data loaders.
- **toxicity/README.md** — toxicity, prompt-injection, moderation classifier package.
- **PII/README.md** — Presidio-backed PII detection package.

For team questions / PR discussion: tag the Signal observability owners in the repo.