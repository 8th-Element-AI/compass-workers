# Compass Workers

Per-lens observability workers that read raw spans from ClickHouse, compute derived metrics, and write them back. One lens = one image = one Deployment (or StatefulSet for Safety). Each lens is independently deployable, scalable, and observable.

```
┌──────────────────────┐    poll       ┌─────────────────────────┐
│  ClickHouse          │ ─────────►    │  Lens Worker            │
│  compass_raw_spans    │               │  (perf / cost / safety) │
└──────────────────────┘               │                         │
        ▲                              │  - Stage 1 gate         │
        │ INSERT                       │  - Per-span context     │
        │ (idempotent via              │  - MetricSpec walk      │
        │  dedup tokens)               │  - Emit derived rows    │
        │                              └─────────────────────────┘
┌──────────────────────┐                         │
│  compass_derived_     │ ◄───────────────────────┘
│  metrics             │
│  → mv_agg_base fires │     ┌──────────────────────────────────┐
│  → aggregated table  │     │  Postgres                        │
└──────────────────────┘     │  worker_checkpoints (per-slot)   │
                             │  *_thresholds (toggle cache)     │
                             │  components (pricing)            │
                             └──────────────────────────────────┘
```

The three lenses available today:

| Lens | Image | Replicas | Notes |
|---|---|---|---|
| **Performance** | `compass-worker:performance` | Deployment, 1 | Latency, errors, retries |
| **Cost** | `compass-worker:cost` | Deployment, 1 | Token spend, waste, pricing from PG |
| **Safety** | `compass-worker:safety` | StatefulSet, 1–16 | PII + toxicity (ML); horizontally scalable |

---

## Table of contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Install](#3-install)
4. [Configuration](#4-configuration)
5. [Run live](#5-run-live)
6. [Run offline (CSV)](#6-run-offline-csv)
7. [Docker images](#7-docker-images)
8. [Observability](#8-observability)
9. [Horizontal scaling](#9-horizontal-scaling)
10. [Production deployment (K8s)](#10-production-deployment-k8s)
11. [Data correctness](#11-data-correctness)
12. [Failure modes](#12-failure-modes)
13. [Adding a new lens](#13-adding-a-new-lens)

---

## 1. Overview

Each worker runs a synchronous poll loop:

```
load checkpoint → fetch batch from CH → process → write derived rows → save checkpoint → repeat
```

- **`fetch`**: `SELECT ... FROM compass_raw_spans WHERE recorded_at > $watermark [AND partition_id = $slot] LIMIT $batch_size`
- **`process`**: Stage 1 gate (drop spans no threshold cares about) → per-span `build_context` → walk `MetricSpec` list → emit rows
- **`write`**: bulk INSERT with a deterministic `insert_deduplication_token`, so any replay drops silently
- **`checkpoint`**: UPSERT into Postgres `worker_checkpoints` keyed by `(lens, partition_key)`

Three lenses share the same engine (`compass_worker.base`, `compass_worker.spec`); they differ only by the `MetricSpec` list, their `build_context` function, and (for Safety) the analyzers they wire into the `PrefillStep` pipeline.

For architecture, design tradeoffs, and rationale, see **`ARCHITECTURE.md`**.

---

## 2. Prerequisites

- Python 3.11+
- ClickHouse reachable (default `localhost:8123`) with the Compass schema applied; see `infra/`
- Postgres reachable (default `localhost:5432`) with the v5 schema seeded and the `worker_checkpoints` table created
- Docker / Docker Compose for local infra
- Kubernetes cluster + `kubectl` for production deployment (StatefulSet, ConfigMap, Secret, Service)
- Disk space: ~6 GB for the Safety Docker image (bakes in ML models + HF cache)

---

## 3. Install

For local dev (host-side):

```powershell
cd E:\8thelement\Compass
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

This installs the worker framework deps (`clickhouse-connect`, `psycopg`, `pydantic-settings`, `prometheus-client`), plus editable installs of the sibling `PII/` and `toxicity/` packages used by the Safety lens.

For container/production builds, see [§7 Docker images](#7-docker-images).

---

## 4. Configuration

All settings are environment-driven via `compass_worker.config.Config` (a pydantic-settings `BaseSettings`). Defaults are sensible for local dev. Override via env vars or a `.env` file.

### Datastore connections

| Env var | Default | Used by | Purpose |
|---|---|---|---|
| `CH_HOST` | `localhost` | all | ClickHouse host |
| `CH_PORT` | `8123` | all | ClickHouse HTTP port |
| `CH_DB` | `compass` | all | ClickHouse database |
| `CH_USER` | `default` | all | ClickHouse user |
| `CH_PASSWORD` | `""` | all | ClickHouse password |
| `PG_DSN` | `postgresql://postgres@localhost:5432/compass` | cost + safety | Postgres DSN (pricing, thresholds, checkpoints) |

### Run loop

| Env var | Default | Purpose |
|---|---|---|
| `WORKER_BATCH` | `5000` | Spans fetched per batch (per slot) |
| `WORKER_POLL_SEC` | `2.0` | Idle backoff between empty fetches |
| `COMPASS_TOGGLE_TTL` | `300` | Toggle cache refresh interval (seconds) |
| `COMPASS_PRICING_TTL` | `300` | Pricing cache refresh interval (seconds; Cost lens only) |

### Horizontal scaling (Phase 4.3)

| Env var | Default | Purpose |
|---|---|---|
| `WORKER_PARTITION_INDEX` | `0` | This pod's index (0-based). Auto-derived from `POD_NAME` in K8s |
| `WORKER_PARTITION_COUNT` | `1` | Total pods sharing the slot space. Must match StatefulSet `replicas` |
| `WORKER_PARTITION_TOTAL_SLOTS` | `16` | Size of the fixed slot space (matches `cityHash64 % N` on `compass_raw_spans.partition_id`) |

Defaults (`COUNT=1`, `INDEX=0`) preserve single-pod behavior — Performance and Cost stay at the defaults; only Safety scales.

### Observability (Phase 5.1)

| Env var | Default | Purpose |
|---|---|---|
| `OBSERVABILITY_PORT` | `8080` | HTTP port for `/healthz` `/readyz` `/metrics` |

### Safety-specific (Safety lens only)

| Env var | Default | Purpose |
|---|---|---|
| `COMPASS_PII_NER_MODEL` | `gravitee-io/bert-small-pii-detection` | HF model id for PII NER |
| `COMPASS_PII_BATCH` | `4` | ThreadPool width for Presidio analyze_batch |
| `COMPASS_PII_CACHE_MAX` | `20000` | LRU cap on per-worker PII content cache |
| `COMPASS_TOXICITY_MODELS_ROOT` | `/opt/models` | Base path for the 4 toxicity model artifacts |
| `COMPASS_TOXICITY_DEVICE` | `cpu` | `cpu` or `cuda` |
| `COMPASS_TOXICITY_BATCH_SIZE` | `32` | Worker-side batched inference width |
| `COMPASS_TOXICITY_FAST_ALLOW` | `0.02` | FastText threshold below which BERT is skipped |
| `COMPASS_TOXICITY_PI_REVIEW` | `0.50` | Prompt-injection review threshold |
| `COMPASS_TOXICITY_HARMFUL_REVIEW` | `0.50` | Harmful-content review threshold |

Full list in `compass_worker/config.py`.

### `.env` files

Local dev:

```bash
# compass-workers/.env
CH_HOST=localhost
CH_PORT=8123
PG_DSN=postgresql://compass:compass@localhost:5432/compass
WORKER_BATCH=5000
```

Docker run:

```bash
# compass-workers/.env.docker
CH_HOST=host.docker.internal
CH_PORT=8123
PG_DSN=postgresql://compass:compass@host.docker.internal:5432/compass
WORKER_BATCH=5000
```

K8s uses a ConfigMap + Secret instead; see [§10](#10-production-deployment-k8s).

---

## 5. Run live

Against the local CH/PG stack:

```powershell
# Single lens, continuous poll loop
python run_worker.py --worker performance
python run_worker.py --worker cost
python run_worker.py --worker safety

# One batch, then exit (smoke test, CI, manual debugging)
python run_worker.py --worker performance --once

# All three lenses in one process — three threads, one PG/CH connection set
# (intended for local dev; production runs each lens in its own container)
python run_worker.py --worker all
```

Stop with `Ctrl+C`. Workers handle SIGINT/SIGTERM gracefully — the in-flight batch completes, the checkpoint is saved, then exit.

---

## 6. Run offline (CSV)

For local development and lens validation, run against an exported spans CSV instead of ClickHouse. Same `compute()` code path; no DB connections required.

```powershell
python run_worker.py --worker safety --csv ./samples/spans.csv --out ./out/safety.csv
```

This is how every new lens is regression-tested before being deployed.

---

## 7. Docker images

Four images, all built from `compass-workers/` Dockerfiles with build context at the repo root.

| Image | Dockerfile | Size | Purpose |
|---|---|---|---|
| `compass-worker:base` | `Dockerfile` | ~400 MB | Internal framework image. Not deployed directly |
| `compass-worker:performance` | `Dockerfile.performance` | ~400 MB | Performance lens entrypoint over `:base` |
| `compass-worker:cost` | `Dockerfile.cost` | ~400 MB | Cost lens entrypoint over `:base` |
| `compass-worker:safety` | `Dockerfile.safety` | ~5.5 GB | Safety lens — extends `:base` with PyTorch, transformers, Presidio, spaCy, the 4 toxicity model artifacts (~1.5 GB), and the PII NER model (~220 MB) |

Build order matters — `:base` first, then the child images that `FROM` it.

```powershell
cd E:\8thelement\Compass
$env:DOCKER_BUILDKIT = "1"

# 1. Base (framework + deps)
docker build -f compass-workers/Dockerfile -t compass-worker:base .

# 2. Performance and Cost (just set the entrypoint)
docker build -f compass-workers/Dockerfile.performance -t compass-worker:performance .
docker build -f compass-workers/Dockerfile.cost        -t compass-worker:cost .

# 3. Safety (heavy ML stack + model downloads — pass HF_TOKEN as BuildKit secret)
$env:HF_TOKEN = "hf_..."
docker build --secret id=hf_token,env=HF_TOKEN -f compass-workers/Dockerfile.safety -t compass-worker:safety .
```

Run any of them with `--env-file`:

```powershell
docker run --rm `
  --env-file compass-workers\.env.docker `
  -p 8080:8080 `
  compass-worker:safety
```

The `-p 8080:8080` exposes the observability endpoints (next section).

---

## 8. Observability

Every worker process runs an HTTP server on port 8080 (configurable via `OBSERVABILITY_PORT`) with three endpoints.

### Endpoints

| Endpoint | Returns | Used for |
|---|---|---|
| `GET /healthz` | `200 ok` (while process is alive) | K8s liveness probe |
| `GET /readyz` | `200 ready` once `run_poll` starts; `503 not ready` before | K8s readiness probe + Service routing during rolling updates |
| `GET /metrics` | Prometheus exposition format | Scrape target |

Quick checks (from the host, while a worker is running):

```powershell
curl.exe http://localhost:8080/healthz
curl.exe http://localhost:8080/readyz
curl.exe http://localhost:8080/metrics | findstr compass_worker
```

> PowerShell aliases bare `curl` to `Invoke-WebRequest`, which is flaky on chunked responses. Use `curl.exe` (Windows 10+ ships it).

### Metrics

All counters / gauges / histograms are labeled by `(lens, slot)`. For unpartitioned lenses (Performance, Cost, or Safety running at `WORKER_PARTITION_COUNT=1`), `slot="all"`. For partitioned lenses, each slot the pod owns produces its own label series.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `compass_worker_batches_total` | counter | `lens, slot, result` | `result` ∈ `success` / `error` / `empty` |
| `compass_worker_spans_processed_total` | counter | `lens, slot` | Spans fetched from CH |
| `compass_worker_rows_emitted_total` | counter | `lens, slot` | Derived rows written |
| `compass_worker_skipped_at_gate_total` | counter | `lens, slot` | Spans dropped at Stage 1 |
| `compass_worker_batch_duration_seconds` | histogram | `lens, slot` | `process_batch()` wall-clock |
| `compass_worker_write_duration_seconds` | histogram | `lens, slot` | CH insert wall-clock |
| `compass_worker_checkpoint_lag_seconds` | gauge | `lens, slot` | `now − last_processed_span.recorded_at` |

### Common queries

```promql
# Are workers healthy?
rate(compass_worker_batches_total{result="success"}[5m]) by (lens)

# Are we keeping up?
compass_worker_checkpoint_lag_seconds

# Throughput per slot — useful for spotting hot slots
rate(compass_worker_spans_processed_total[5m]) by (lens, slot)

# Error rate
rate(compass_worker_batches_total{result="error"}[5m]) by (lens) /
rate(compass_worker_batches_total[5m]) by (lens)

# Per-lens p95 batch latency
histogram_quantile(0.95,
  rate(compass_worker_batch_duration_seconds_bucket[5m])
) by (lens)
```

### Scaling compass

`compass_worker_checkpoint_lag_seconds{lens="safety"}` is the input for an HPA on the Safety StatefulSet (when you're ready to automate scaling). Three regimes:

| Lag pattern | What it means | Action |
|---|---|---|
| Flat near 0–10s | Keeping up; happy path | None |
| Stable at minutes | At capacity but tracking inflow | Acceptable if SLO allows; otherwise scale |
| Growing without bound | Below inflow rate; falling behind forever | Scale (`scale-safety.ps1`) |

---

## 9. Horizontal scaling

**Performance and Cost** run as single-pod Deployments — they're CPU-cheap and rarely bottleneck. They stay at `replicas: 1` with `WORKER_PARTITION_COUNT=1`.

**Safety** runs as a StatefulSet with 1–16 replicas. Each replica owns a fixed slice of the 16-slot virtual partition space.

### The slot model

`compass_raw_spans` has a materialized column:

```sql
partition_id UInt8 MATERIALIZED cityHash64(trace_id) % 16
```

Plus a set-typed skip index on `partition_id` so per-slot fetches are efficient. The column is computed once at ingestion and never recomputed — adding/removing pods doesn't re-hash anything.

Each Safety pod owns a deterministic subset:

| Pod count | Pod 0 owns | Pod 1 owns | … | Pod N-1 owns |
|---|---|---|---|---|
| 1 | {0..15} | — | — | — |
| 2 | {0..7} | {8..15} | — | — |
| 4 | {0..3} | {4..7} | {8..11} | {12..15} |
| 8 | {0,1} | {2,3} | … | {14,15} |
| 16 | {0} | {1} | … | {15} |

Uneven divisions (e.g. `N=3`) work too — the first `(16 % N)` pods each get one extra slot. Power-of-2 divisions are recommended for even load.

### Per-slot watermarks

Each slot has its own row in `worker_checkpoints` keyed by `partition_key = "slot:N"`:

```sql
SELECT lens, partition_key, watermark, updated_by
FROM worker_checkpoints
WHERE lens = 'safety'
ORDER BY partition_key;

--   lens   | partition_key |        watermark        |       updated_by
-- --------+---------------+-------------------------+-------------------------
--  safety | slot:0        | 2026-06-18 12:00:00.000 | compass-worker-safety-0
--  safety | slot:1        | 2026-06-18 12:00:00.000 | compass-worker-safety-0
--  safety | slot:2        | 2026-06-18 12:00:00.000 | compass-worker-safety-0
--  safety | slot:3        | 2026-06-18 12:00:00.000 | compass-worker-safety-0
--  safety | slot:4        | 2026-06-18 11:58:00.000 | compass-worker-safety-1
--  ...    | ...           | ...                     | ...
```

When pod count changes, **slot watermarks survive** — a new pod inheriting slot 7 reads slot 7's watermark and resumes from there. **No manual rebalancing. No human PG edits.** That's the whole point of the slot model.

Single-pod deployments (default) use `partition_key = "default"` instead of slot rows — fully backward compatible.

### Scaling Safety

Use `infra/k8s/scale-safety.ps1` — it atomically patches `replicas` AND `WORKER_PARTITION_COUNT` in one operation, watches the rollout, and verifies expected slot watermarks materialize in PG.

```powershell
# Scale to 4 pods (each owns 4 slots)
.\infra\k8s\scale-safety.ps1 -Replicas 4

# Scale up to 8
.\infra\k8s\scale-safety.ps1 -Replicas 8

# Scale back down to 2 (no data loss; watermarks survive)
.\infra\k8s\scale-safety.ps1 -Replicas 2
```

**Never edit `replicas` without also updating `WORKER_PARTITION_COUNT`** — they MUST match exactly. The script enforces this. Manual `kubectl scale` will desync them and produce wasted compute (data correctness is still preserved via dedup tokens, but pods will overlap).

### Local two-pod simulation

To exercise the partitioned path without K8s, use Docker directly:

**Terminal A** (slots 0–7):
```powershell
docker run --rm `
  -e WORKER_PARTITION_INDEX=0 `
  -e WORKER_PARTITION_COUNT=2 `
  --env-file compass-workers\.env.docker `
  -p 8080:8080 `
  compass-worker:safety
```

**Terminal B** (slots 8–15):
```powershell
docker run --rm `
  -e WORKER_PARTITION_INDEX=1 `
  -e WORKER_PARTITION_COUNT=2 `
  --env-file compass-workers\.env.docker `
  -p 8081:8080 `
  compass-worker:safety
```

You'll see partition info in each pod's startup log:

```
[safety] partition: pod=0/2 owns_slots=[0, 1, 2, 3, 4, 5, 6, 7]
[safety] starting poll loop (batch=5000 slots=[0, 1, 2, 3, 4, 5, 6, 7])
```

Metrics from each pod will be labeled with that pod's slots:

```powershell
curl.exe http://localhost:8080/metrics | findstr 'slot="0"'
curl.exe http://localhost:8081/metrics | findstr 'slot="8"'
```

PG will have 16 slot rows once both pods have processed at least once per slot.

---

## 10. Production deployment (K8s)

All manifests live under `infra/k8s/`. Numbered so `kubectl apply -f infra/k8s/` applies them in dependency order:

```
infra/k8s/
├── 00_namespace.yaml              compass namespace
├── 10_configmap.yaml              shared non-secret env (CH host, batch size, slot count, etc.)
├── 11_secret.yaml                 PG_DSN, CH_PASSWORD (REPLACE PLACEHOLDERS BEFORE APPLYING)
├── 20_deployment-performance.yaml Performance lens (replicas=1)
├── 21_deployment-cost.yaml        Cost lens (replicas=1)
├── 30_statefulset-safety.yaml     Safety lens (replicas=1..16, partitioned)
├── 40_service-metrics.yaml        Headless services for Prometheus + StatefulSet DNS
└── scale-safety.ps1               Automated rebalancing script
```

### Deploy

```powershell
# 1. Substitute placeholders in 11_secret.yaml (PG_DSN, CH_PASSWORD)
notepad infra\k8s\11_secret.yaml

# 2. Apply
kubectl apply -f infra/k8s/

# 3. Watch all three workers come up
kubectl get pods -n compass -w
```

You should see:

```
NAME                                          READY   STATUS    RESTARTS   AGE
compass-worker-performance-7d4c5f9b9-xz9kj     1/1     Running   0          30s
compass-worker-cost-6f8b4d7d8-mn2vh            1/1     Running   0          30s
compass-worker-safety-0                        1/1     Running   0          30s
```

(Note the StatefulSet ordinal `-0` on Safety vs the random hashes on Performance/Cost.)

### Scale Safety

```powershell
cd infra/k8s
.\scale-safety.ps1 -Replicas 4
```

The script:

1. Atomically patches `spec.replicas` + `WORKER_PARTITION_COUNT` env in one JSON patch.
2. Watches `kubectl rollout status` until rollout completes.
3. Polls Postgres until all expected slot watermark rows materialize in `worker_checkpoints`.
4. Reports success with the new pod list, or warns if any slots are missing (typically because spans haven't arrived for that slot yet).

For HPA-driven autoscaling (later phase), use `compass_worker_checkpoint_lag_seconds` as the input metric.

### Prometheus scraping

All worker pods carry pod annotations for auto-discovery:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8080"
  prometheus.io/path: "/metrics"
```

For Prometheus Operator users, add a `ServiceMonitor` selecting on `app=compass-worker`.

### Connection to PG/CH from K8s pods

By default the manifests assume PG and CH are reachable at:

```yaml
CH_HOST: "clickhouse.compass.svc.cluster.local"
PG_DSN:  "postgresql://compass:CHANGEME@postgres.compass.svc.cluster.local:5432/compass"
```

Adjust per environment:

| Environment | CH/PG location | Host string |
|---|---|---|
| Same K8s cluster | In-cluster service | `<svc>.<ns>.svc.cluster.local` |
| Docker Desktop K8s + host containers | Host Docker network | `host.docker.internal` |
| Managed services (GCP / AWS) | External endpoint | Per cloud provider |

Verify connectivity from a worker pod:

```powershell
kubectl exec -n compass compass-worker-safety-0 -- `
  python -c "import urllib.request; print(urllib.request.urlopen('http://`$CH_HOST:`$CH_PORT/ping').read())"
```

---

## 11. Data correctness

The pipeline guarantees:

- **Append-only writes.** Workers only `INSERT` into `compass_derived_metrics`; they never UPDATE or DELETE.
- **Idempotency under restart.** Each insert carries a deterministic `insert_deduplication_token` of the form `{lens}:s{slot}:{newest_recorded_at}:{batch_size}`. ClickHouse's `non_replicated_deduplication_window = 1000` setting drops duplicate tokens silently — and critically, the materialized view does NOT fire on a dropped insert. So crash-and-replay produces no double-counts.
- **Per-lens isolation.** Each lens has its own checkpoint row(s), its own image, its own Deployment/StatefulSet. A slow or broken Safety pod does not affect Performance or Cost.
- **Slot watermarks survive scaling.** Per-slot rows in `worker_checkpoints` persist across pod count changes. Adding or removing replicas reassigns ownership but never loses progress.

What's *not* guaranteed:

- **Exactly-once across CH and PG.** No shared transaction. We settle for effective once-per-row in CH via dedup; PG watermark can technically lag.
- **Strict inter-batch ordering.** Each batch is atomic, but `ORDER BY recorded_at LIMIT N` can skip rows that share the boundary timestamp if a tie spans the batch edge (mitigated by `WORKER_BATCH=5000`).
- **Concurrent multi-pod on the same slot.** Don't override the scale script with `kubectl scale` — the script keeps `replicas` and `WORKER_PARTITION_COUNT` in sync. Manual scaling can desync them, causing wasted compute.

See `ARCHITECTURE.md` § 9 for the full failure-mode table.

---

## 12. Failure modes

### Pod restarts mid-batch

`process_batch` is interrupted → no `write()` happens → checkpoint doesn't advance → restart re-fetches the same spans → dedup token matches the un-written attempt (or matches the prior successful write, depending on where the crash hit) → CH drops the duplicate insert if any → MV doesn't fire. **No data loss, no double-count.**

### PG unavailable

Checkpoint save/load fails → exception propagates → pod restarts → load retries on next boot. Worker is unavailable until PG recovers, but no data is lost (CH spans wait for the worker to come back).

### CH unavailable

Fetch fails → exception → restart. Same recovery semantics.

### Safety models fail to load

Pod fails readiness check → K8s doesn't route traffic to it → liveness check fails after threshold → pod restarts. If model files are corrupt (bad image), all replicas fail — roll back to previous image.

### Slow batches blocking the run loop

By design — the loop is synchronous. If Safety is consistently slow:

1. Check `compass_worker_batch_duration_seconds` p95 vs the rate of incoming spans.
2. Check `compass_worker_checkpoint_lag_seconds` — if growing, you're below capacity.
3. Scale up via `scale-safety.ps1 -Replicas N` (up to 16).

### Wasted compute during rolling update

When you scale, the StatefulSet rolls pods one at a time. For ~30s during the rollout, slot ownership is briefly inconsistent (some pods see the new COUNT, others the old). Dedup tokens preserve data correctness; the only cost is wasted CPU on those slots. For minimal disruption, prefer `podManagementPolicy: Parallel` (already set in `30_statefulset-safety.yaml`).

---

## 13. Adding a new lens

1. Create `compass_worker/lenses/<name>.py` subclassing `SpecWorker`:

   ```python
   class MyLensWorker(SpecWorker):
       lens = "mylens"
       SPECS = [...]                          # MetricSpec list
       span_types = ("model_call",)           # optional CH-side prefilter
 
       def build_context(self, span):
           # Parse metadata, do math, return a dict
           return {"my_value": ...}
   ```

2. Register in `run_worker.py` `LENSES` dict:

   ```python
   LENSES = {
       "performance": PerformanceWorker,
       "cost":        CostWorker,
       "safety":      SafetyWorker,
       "mylens":      MyLensWorker,
   }
   ```

3. If the lens needs its own image (heavy deps), create `Dockerfile.mylens` based on `:base`. Otherwise `:base` itself can run any lens via `--worker mylens`.

4. If the lens needs new threshold dimensions, add them to `infra/postgres/init/02_thresholds.sql` and seed.

5. Validate offline against a CSV (`--csv`) before deploying.

6. Add a Deployment or StatefulSet manifest under `infra/k8s/`. Performance-like → Deployment. Safety-like (slow ML, needs scaling) → StatefulSet + `scale-mylens.ps1`.

Reference implementations:

- Simple stateless: `lenses/performance.py` — 14 specs, ~30 lines of `build_context`
- With PG dependency: `lenses/cost.py` — pricing cache + 22 specs
- Heavy ML + slot scaling: `lenses/safety.py` — PrefillStep + lazy models + StatefulSet

---

## Appendix — Related docs

- **`ARCHITECTURE.md`** — design rationale, schemas, tradeoffs, failure-mode tables
- **`infra/README.md`** — Postgres + ClickHouse compose stack, schemas
- **`PII/README.md`** — the Presidio-backed PII detection package (Safety dependency)
- **`toxicity/README.md`** — the FastText + DeBERTa toxicity classifier (Safety dependency)
- **`infra/k8s/scale-safety.ps1`** — the scaling automation script