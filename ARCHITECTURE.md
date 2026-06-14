# Signal Workers — Architecture

The deep technical reference for the Signal observability workers. This document
covers **why** the system is shaped this way, what each design decision costs,
and what's deferred and why. For **how** to install / configure / deploy, see
`README.md`. For data store internals, see `infra/README.md`.

```
Audience: engineers extending the worker framework, reviewing data correctness
          claims, debugging production incidents, or making scaling decisions.

Not in scope: model training (that's the safety-classifier and other repos),
              the consumer API on top of ClickHouse, the ingestion path that
              fills signal_raw_spans.
```

---

## Table of contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [System context](#2-system-context)
3. [Storage architecture — two stores, one shape](#3-storage-architecture--two-stores-one-shape)
4. [ClickHouse schema deep-dive](#4-clickhouse-schema-deep-dive)
5. [Postgres schema deep-dive](#5-postgres-schema-deep-dive)
6. [The worker framework](#6-the-worker-framework)
7. [The lenses](#7-the-lenses)
8. [Deployment architecture](#8-deployment-architecture)
9. [Data correctness guarantees](#9-data-correctness-guarantees)
10. [Observability](#10-observability)
11. [Tradeoffs and deferred work](#11-tradeoffs-and-deferred-work)
12. [Entity model glossary](#12-entity-model-glossary)
13. [Future evolution](#13-future-evolution)

---

## 1. Goals and non-goals

### Goals

| Goal | Concrete shape |
|---|---|
| **One metric registry per "lens"** (`performance`, `cost`, `safety`, …) | Each lens is its own Python package, its own Docker image, its own K8s Deployment, and its own checkpoint row in Postgres |
| **Add a metric in one line** | The spec model: a metric is a `MetricSpec(applies, pattern, …)` declaration over a small shared library of patterns and predicates |
| **Append-only writes, idempotent under restart** | Workers only `INSERT` into `signal_derived_metrics`; the materialized view does aggregation. CH insert-deduplication makes crashes and replays safe |
| **Scale lenses independently** | Lenses share no state at runtime; Performance can lag without affecting Safety |
| **Run the same compute offline** | The `--csv` path runs the identical `compute()` against an exported spans file with no DB |
| **Production-deployable on Kubernetes** | Per-lens Docker images, PG-backed checkpoints (no shared filesystem needed), `/healthz` + `/readyz` + `/metrics` HTTP endpoints, graceful SIGTERM drain |

### Non-goals

- **Sub-second ingestion latency.** The system is a poll-based observability
  pipeline, not a real-time streaming engine. Typical end-to-end lag from span
  write to derived metric is 2–5 seconds.
- **Exactly-once semantics across both stores.** ClickHouse and Postgres don't
  share a transactional boundary; we settle for **effective once-per-row in
  ClickHouse** via insert deduplication. See [§9](#9-data-correctness-guarantees).
- **A guardrail / blocking layer.** The Safety lens emits *observability*
  signals (PII counts, toxicity labels). It does not block requests. The
  application layer decides what to do with the signals.
- **Multi-tenant isolation at the worker level.** Tenants share the same
  worker process; isolation is enforced at the data model (every row carries
  `solution_id`).
- **A general-purpose stream processor.** This is purpose-built for the Signal
  schema. Not a Flink/Spark replacement.

---

## 2. System context

```
   ┌─────────────────────────┐
   │  Span Ingestion         │  ← writes raw spans (NATS / OTel collector / direct)
   │  (not in this repo)     │
   └────────────┬────────────┘
                │
                ▼
   ┌──────────────────────────────────────┐
   │  ClickHouse                          │
   │  ┌────────────────────────────────┐  │
   │  │ signal_raw_spans (MergeTree)   │  │
   │  └────────────────────────────────┘  │       Postgres
   └──────────────┬───────────────────────┘  ┌─────────────────────────────┐
                  │ poll (recorded_at > wm)  │ solutions / endpoints /     │
                  │                          │ workflows / agents /        │
                  ▼                          │ components / bindings       │
   ┌──────────────────────────────────────┐  │ thresholds                  │
   │  Lens Worker  (perf / cost / safety) │──┤ worker_checkpoints          │
   │  ┌────────────────────────────────┐  │  │ pricing (in components)     │
   │  │ build_context(span)            │  │  └─────────────────────────────┘
   │  │ for spec in lens.SPECS:        │  │           ▲
   │  │   if applies and gated:        │  │           │ read thresholds (TTL cached)
   │  │     row = pattern(span, ctx)   │  │           │ read pricing (Cost)
   │  └────────────────────────────────┘  │           │ UPSERT watermark
   └──────────────┬───────────────────────┘           │
                  │ INSERT (with dedup token)         │
                  ▼                                   │
   ┌──────────────────────────────────────┐           │
   │  ClickHouse                          │           │
   │  ┌────────────────────────────────┐  │           │
   │  │ signal_derived_metrics         │  │           │
   │  │ + dedup window = 1000          │  │           │
   │  └─────────────┬──────────────────┘  │           │
   │                │ MV fires on insert  │           │
   │                ▼                     │           │
   │  ┌────────────────────────────────┐  │           │
   │  │ signal_aggregated_metrics      │  │           │
   │  │ (1-min buckets, AggregatingMT) │  │           │
   │  └────────────────────────────────┘  │           │
   └──────────────────────────────────────┘           │
                  ▲                                   │
                  │ reads                             │
                  │                                   │
   ┌──────────────────────────────────────────────────┴─────┐
   │  Consumer API  (dashboards, alerts, exports)           │
   │  (not in this repo)                                    │
   └────────────────────────────────────────────────────────┘
```

The workers sit between ClickHouse-as-truth (spans in) and ClickHouse-as-rollup
(metrics out). Postgres exists alongside as the "what should be" store —
registry, configured thresholds, prices, and worker high-watermarks.

---

## 3. Storage architecture — two stores, one shape

ClickHouse is the **analytical engine** — everything that happened. Postgres is
the **operational store** — everything that exists or should be.

| Postgres (alongside) | ClickHouse (this pipeline) |
|---|---|
| What entities exist (`solutions`, `endpoints`, `workflows`, `agents`, `components`) | What happened (`signal_raw_spans`) |
| What's wired to what (`bindings`) | What was measured (`signal_derived_metrics`) |
| What the limits are (`*_thresholds`) | Pre-aggregated time series (`signal_aggregated_metrics`) |
| Component pricing (`components.pricing` JSONB) | |
| Worker watermarks (`worker_checkpoints`) | |

### The materialized-path convention

Both stores share one mental model: a row carries the **full entity path** plus
a `scope` saying which level is the target.

```
solution_id  →  endpoint  →  workflow_id  →  agent_id  →  component_id  →  component_type
                                                                              ^ deepest
```

Rule: **the deepest non-empty id is the target; higher levels are context.**

This applies to:
- `bindings` in Postgres ("in this context this asset runs with this config" — deepest non-NULL FK = the target)
- `*_thresholds` in Postgres (the path defines what entity the alert applies to)
- `signal_derived_metrics` / `signal_aggregated_metrics` scope columns

One enforcement function, `path_cols(span, scope)`, blanks the ids deeper than
the scope:

| scope | keeps | blanks |
|---|---|---|
| solution / endpoint | `solution_id` (+ `endpoint`) | workflow, agent, component |
| workflow | + `workflow_id` | agent, component |
| agent | + `agent_id` | component |
| component | full path | — |

The same function runs in both lens compute (when emitting rows) and threshold
gating (when checking if a span is "of interest"). One bug to fix, one mental
model.

---

## 4. ClickHouse schema deep-dive

### 4.1 `signal_raw_spans` — "what happened"

- **Engine**: `MergeTree`
- **Partition**: `toYYYYMM(started_at)` (monthly)
- **Order key**: `(solution_id, span_type, started_at, trace_id, span_id)`
- **TTL**: 90 days from `started_at`

25 columns. Lean and denormalized: entity path, classification (`span_type`,
`span_status`, `scope`), timing (`started_at`, `ended_at`), infra metadata
(`service`, `environment`, `region`), and one `metadata` String (`CODEC ZSTD(3)`)
holding everything else.

**Why one JSON blob instead of typed columns**: span shapes differ wildly by
`span_type`. A `model_call` carries token usage, model id, IO text. A `tool_call`
carries function name, args. A `retrieval` carries query, chunks, scores. Forcing
every column for every span type leads to 90% NULL columns. The JSON blob keeps
the table lean; lenses parse what they need via `parse_meta`.

**Why `span_type` is the second column of the order key**: it lets `WHERE
span_type IN (...)` push into the primary-key index. The Cost lens has
`span_types = ("model_call", "embedding", "tool_call", "retrieval")` and never
reads the other 7 span types. The Safety lens does `span_types = ("model_call",)`.

### 4.2 `signal_derived_metrics` — "what was measured"

- **Engine**: `MergeTree`
- **Partition**: `toYYYYMMDD(ts)` (daily)
- **Order key**: `(solution_id, scope, metric, ts, component_id)`
- **TTL**: 90 days from `ts`
- **Settings**: `non_replicated_deduplication_window = 1000`

EAV (entity-attribute-value): one row per metric per span. 18 columns:

- Entity path columns (matching `path_cols(span, scope)` output).
- `scope`, `environment`.
- `ts`, `metric`, `value`, `confidence`, `metric_meta`.
- `start_ts` / `end_ts` so lenses that span time windows (`latency`, `retry_delay`) carry their range.
- `trace_id`, `parent_span_id` denormalized in for per-trace queries without a join back to raw.

**Workers only ever write here.** Append-only by contract.

#### The dedup window

`non_replicated_deduplication_window = 1000` is what makes the worker
restart-safe (see [§9.2](#92-idempotency-via-dedup-tokens)). ClickHouse
remembers the last 1000 block hashes / tokens inserted into this table and
silently drops any duplicate. **Critically: the MV does not fire on a
deduplicated insert.** This is what protects the aggregated table from double-
counting on replay.

#### Why daily partitioning

Daily partitions match the worker's write cadence (continuous, but TTL/cleanup
operates daily). Monthly would make per-day queries scan too much; hourly would
explode the number of parts on a busy cluster.

### 4.3 `signal_aggregated_metrics` — "the pre-rolled time series"

- **Engine**: `AggregatingMergeTree`
- **Partition**: `toYYYYMM(ts)` (monthly)
- **Order key**: `(solution_id, scope, metric, ts, workflow_id, agent_id, component_id, component_type, environment)`
- **TTL**: 365 days from `ts`

This is the primary read table for the dashboard layer, and it's the subtle one.
It stores **aggregate-function states**, not finished numbers:

| Column | Type | Read with |
|---|---|---|
| `count`, `sum_value`, `min_value`, `max_value` | `SimpleAggregateFunction` | direct; re-apply `sum/min/max` when grouping |
| `avg_value` | `AggregateFunction(avg, Float64)` | `avgMerge(avg_value)` |
| `quantiles` | `AggregateFunction(quantilesTDigest(0.5,0.95,0.99), Float64)` | `quantilesTDigestMerge(...)(quantiles)` → `[p50,p95,p99]` |
| `avg_confidence` | `AggregateFunction(avg, Nullable(Float32))` | `avgMerge(avg_confidence)` |

**Why states instead of finished averages**: averages don't sum, percentiles
*really* don't sum. You can't average two pre-averaged buckets and get the right
answer, and you can't add two p95s. By storing the intermediate aggregate state
(a t-digest sketch for quantiles, a sum+count pair for avg), ClickHouse can
merge any set of base buckets into a correct coarser-window result at read
time.

**The gotcha**: selecting a state column raw prints binary garbage. You must
read through the matching `*Merge` combinator with a `GROUP BY`. The consumer
API enforces this; ad-hoc queries get a surprise.

### 4.4 The aggregation layer — one MV, one base grain

There is exactly **one** materialized view, `mv_agg_base`, at a **1-minute**
base grain. It fires on every insert into `signal_derived_metrics`:

```
INSERT into signal_derived_metrics
        │
        ▼
   mv_agg_base   ── groups by (entity path, scope, metric, 1-min bucket)
        │           and writes count/sum/min/max + avgState + quantilesTDigestState
        ▼
signal_aggregated_metrics   (one row per entity × metric × minute)
```

Coarser windows (5m, 1h, 1d) are **not separate tables or views** — they're
produced by merging base-grain buckets at read time:

```sql
-- 1h view = merge 60 one-minute buckets
SELECT toStartOfHour(ts) AS hour,
       avgMerge(avg_value),
       quantilesTDigestMerge(0.95)(quantiles)
FROM signal_aggregated_metrics
WHERE metric = 'latency' AND solution_id = 'sol_support'
GROUP BY hour;
```

**Why this matters**: no separate rollup pipeline, no "missing 15m buckets"
incidents, no n^2 explosion of MVs. To support sub-minute live tailing, change
the MV's `INTERVAL 1 MINUTE` to `30 SECOND` — that's the only knob.

### 4.5 Why no `ReplacingMergeTree` on derived

A common instinct is "switch derived to `ReplacingMergeTree` keyed on
`(span_id, metric, scope)` so duplicate inserts merge cleanly." It doesn't work
**for the MV path**:

1. `ReplacingMergeTree` deduplicates at *merge time*, not at insert time.
2. The MV fires on every insert, before any merge.
3. So two inserts of the same row produce two aggregate-state rows in the
   target table — even if the source table later cleans itself up.

The right fix is to prevent the duplicate insert in the first place, which is
what `insert_deduplication_token` does (and why the MV correctly does not fire
on a dropped insert). See [§9.2](#92-idempotency-via-dedup-tokens).

---

## 5. Postgres schema deep-dive

### 5.1 The seven core tables

```
solutions ──< endpoints
            └ < workflows
            └ < agents
            └ < components (model | tool | skill | function | knowledgebase | memory)
                  └ pricing : JSONB
                  └ metadata : JSONB

bindings        : "in this (solution, endpoint, workflow?, agent?) context, this asset runs with this config"
                  deepest non-NULL FK is the target

*_thresholds    : alert limits for a metric at a scope
                  scope ∈ (solution, endpoint, workflow, agent, component)
                  category ∈ (performance, quality, cost, safety, outcomes)
```

Three enums:
- `component_type_enum` = `model | tool | skill | function | knowledgebase | memory`
- `threshold_category_enum` = `performance | quality | cost | safety | outcomes`
- `scope_level_enum` = `solution | endpoint | workflow | agent | component`

### 5.2 `worker_checkpoints` (new)

One row per `(lens, partition_key)`. UPSERT'd by every worker batch.

```sql
CREATE TABLE worker_checkpoints (
    lens          TEXT        NOT NULL,
    partition_key TEXT        NOT NULL DEFAULT 'default',
    watermark     TEXT        NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS.mmm'
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT,                          -- pod hostname (diagnostic)
    PRIMARY KEY (lens, partition_key)
);
```

`partition_key` is reserved for [future hash-partitioned consumption](#114-horizontal-scaling-safety).
Always `'default'` today.

### 5.3 Why use both Postgres and ClickHouse

You could imagine putting everything in one. We don't, because:

| Concern | Postgres | ClickHouse |
|---|---|---|
| Strong transactional updates to small rows (e.g. flip a threshold) | ✅ native | ❌ designed for analytical inserts |
| 100M+ row analytical queries with sub-second latency | ❌ would need heroic indexing | ✅ what it's built for |
| Indexed lookups by single PK | ✅ | works but not the strength |
| JSON column queries with mid-cardinality | ✅ jsonb_path_ops | works but worse |
| Bulk insert throughput | hundreds of rows/sec | millions of rows/sec |

The registry is small (thousands of rows, lots of UPDATEs), the telemetry is
huge (hundreds of millions of rows, append-only). Use the right tool for each.

The cost is a foreign-key boundary you can't enforce at the DB level: a span in
ClickHouse references a `solution_id` that lives in Postgres. The platform
treats the IDs as immutable strings; deletes in Postgres don't cascade to CH.
Worth it for the orders-of-magnitude perf difference on the analytical side.

---

## 6. The worker framework

### 6.1 Code arrangement: engine vs lens

The package is deliberately split into a **shared engine** and **thin lenses**:

```
signal_worker/
  base.py          ENGINE  — run loop, CH I/O, checkpoint, CSV path
  spec.py          ENGINE  — MetricSpec + SpecWorker (build-context → walk-specs → emit)
  checkpoint.py    ENGINE  — Postgres-backed checkpoint store
  observability.py ENGINE  — HTTP server + Prometheus metrics
  toggle_cache.py  ENGINE  — Stage-1 gate cache (PG-backed, TTL refresh)
  config.py        SHARED  — Pydantic Settings (one source of truth, env-driven)
  patterns.py      SHARED  — the handful of compute shapes every metric reuses
  predicates.py    SHARED  — the "which spans" filters
  pricing.py       SHARED  — PricingCache (Cost lens reads from PG)
  utils.py         SHARED  — LRUCache, helpers

  lenses/
    performance.py  LENS — 14 specs + build_context
    cost.py         LENS — 22 specs + build_context (+ span_types filter)
    safety.py       LENS —  4 specs + lazy ML pipeline (PII + toxicity)
```

The guiding principle: **a metric is a thin declarative spec over a small
shared compute library, plus a per-span context built once.** ~100 metrics
across the five lenses collapse to ~6 compute patterns. Adding a metric is
usually one line.

### 6.2 The spec model (`spec.py`)

```python
@dataclass
class MetricSpec:
    metric: str          # e.g. "latency"
    lens: str            # "performance"
    applies: Callable    # (span) -> bool          — a predicate
    pattern: Callable    # (span, ctx) -> float|None — a compute pattern
    inputs: list         # documentation: what it reads
    unit: str            # "ms", "USD", "count", ...
    window: str          # rollup window hint ("5m", "1h", "1d")
    threshold: bool      # does Postgres define an alert for it
    per_span: bool       # False => computed at READ time, not by the worker
    meta_fn: Callable    # (span, ctx) -> dict|None   — optional per-row metric_meta
```

A spec is **pure declaration** — no logic in the spec itself. `applies` and
`pattern` are picked from `predicates.py` and `patterns.py`. A `pattern`
returning `None` means "nothing to emit for this span" (an absent metadata
field, a divide-by-zero guard) — the engine simply skips it.

**Patterns** (`patterns.py`) are the reusable compute shapes:

- `column_latency()` → ended − started
- `status_flag(set)` → 1 if `span_status` in set, else 0
- `metadata_numeric(key)` / `metadata_bool(key)` → pull from the JSON blob
- `ratio(num, den)` → divide, None on zero
- `ctx_value(field)` → read a value the context-builder already computed
- `aggregation_derived()` → marker for read-time metrics; always returns None

**Predicates** (`predicates.py`) are the "which spans" filters:

- `any_span`, `llm_call` (`span_type == 'model_call'`)
- `billable` (`model_call | embedding | tool_call | retrieval`)
- `retryable`, `rate_limited`, `batch_op`
- `queued_op`, `orchestrated`
- `sol_wf`, `levels` (for the read-time gauges)

### 6.3 The compute engine (`SpecWorker.compute`)

For each span:

```python
compute(span):
  if span_types set and span.span_type not in span_types:
      return []                           # cheap reject before context build
  ctx = build_context(span)               # lens-specific: parse metadata, do math, ONCE
  for scope in scopes_for(span):          # usually one; solution spans mirror to endpoint
      p = path_cols(span, scope)          # blank ids deeper than this scope
      for spec in specs:
          if not spec.per_span:           # read-time metrics aren't emitted here
              continue
          if not spec.applies(span):
              continue
          if (scope, p, spec.metric) not in active_toggles:   # Stage 2 gate
              continue
          val = spec.pattern(span, ctx)
          if val is None:                 # nothing to emit
              continue
          rows.append(row(p, span, spec, val, ctx))
  return rows
```

**`build_context` runs once per span** even though many specs read from it.
That's the whole efficiency idea: parse the metadata once, do the pricing math
once, run the model inference once. The Safety lens takes this further with
batched pre-fill across many spans (next section).

### 6.4 The two-stage gate

Lenses can be expensive (Safety loads ML models). It would be wasteful to do
that work for spans the platform doesn't care about. So the engine has **two
gates**:

| Stage | Where | What it checks | Why |
|---|---|---|---|
| **Stage 1** | `filter_spans_by_gate` (before compute) | Span has *any* active threshold for this lens | Drop spans we'd emit nothing for, before doing expensive context-building |
| **Stage 2** | inside `compute()`, per (scope, path, metric) | This *specific* (scope, path, metric) has an active threshold | Drop emissions to metrics no threshold cares about |

Stage 1 is the big win for Safety. If 90% of `model_call` spans belong to
solutions with no active safety thresholds, Stage 1 drops them before Presidio
or the toxicity classifier ever sees them.

Both stages consult the same `ToggleCache` — a PG-backed set of active
`(scope, solution_id, endpoint, workflow_id, agent_id, component_id, metric)`
tuples, refreshed on a TTL. Reads are O(1) (set membership). The cache
self-refreshes from Postgres `*_thresholds` rows every `SIGNAL_TOGGLE_TTL`
seconds (default 300).

### 6.5 The PrefillStep pipeline (expensive batch analyses)

When a lens has expensive per-text inference (Safety: PII detection, toxicity
classification), per-span calls are wasteful — many spans share identical
prompts. The engine provides a generic batched pipeline:

```python
@dataclass
class PrefillStep:
    name: str                             # log label
    metrics: Set[str]                     # metric names that need this step
    cache: LRUCache                       # result cache populated here
    extract: Callable                     # span -> iterable of texts
    analyze: Callable                     # texts -> list of results
```

Per batch:

1. **Sub-filter** to spans whose threshold metrics overlap `step.metrics` (so a
   span kept only for *another* group's threshold doesn't pay this step's CPU).
2. **Extract** texts via `step.extract`.
3. **Dedup** against `step.cache` (content hash) and within the batch.
4. **Analyze** the unique remaining texts via `step.analyze`.
5. **Cache** results by content hash.

The result cache is huge: the same system prompt appears in thousands of spans,
so we analyze it once and cache the result. Hit rates in production are
typically 95%+ for established workloads.

The Safety lens registers two PrefillSteps (`pii`, `toxicity`); Quality (future)
will register its judge-call steps the same way.

### 6.6 Scoping and the materialized path

Every derived row carries the full entity path. The `scope` column says which
level the metric is *about*.

Per [§3](#3-storage-architecture--two-stores-one-shape), `path_cols(span, scope)`
blanks ids deeper than the scope. `scopes_for(span)` returns the scope(s) the
span emits at — usually one. The exception: a root `solution` span also mirrors
to `endpoint` so endpoint-scoped dashboards have data.

### 6.7 The run loop

```
run_poll():
  set_ready(True)
  loop:
    wm    = load_checkpoint()                       # PG: SELECT watermark FROM worker_checkpoints
    spans = fetch_batch(since=wm, limit=batch)      # CH: recorded_at > wm ORDER BY recorded_at LIMIT N
    if not spans:
      sleep(poll_sec); continue
    rows  = process_batch(spans)                    # gate → compute → emit
    newest = max(s.recorded_at for s in spans)
    dedup_token = f"{lens}:{newest}:{len(spans)}"
    write(rows, dedup_token=dedup_token)            # CH: INSERT INTO derived_metrics (atomic dedup)
    save_checkpoint(newest)                         # PG: UPSERT worker_checkpoints
    if --once: break
```

Three entry paths share the **same** `compute()`:

- **`run_poll`** — production: poll ClickHouse by a `recorded_at` checkpoint.
- **`run_csv`** — offline: run the identical logic over an exported spans CSV.
  This is how every lens is validated.
- **`on_message`** — a NATS-shaped hook for an event-driven future (currently a
  stub).

**ClickHouse and PG connections are lazy** — opened only when actually used —
so the offline CSV path needs neither driver loaded.

### 6.8 Checkpoint store

`checkpoint.py` is a single class: `PostgresCheckpointStore`. The historical
file-based store was removed when the workers moved to K8s (no shared
filesystem between pods).

```python
class PostgresCheckpointStore:
    PARTITION_KEY = "default"   # always 'default' until partitioned consumption lands

    def load(self, lens: str) -> str:
        # SELECT watermark FROM worker_checkpoints WHERE lens=%s AND partition_key=%s
        # Returns '1970-01-01 00:00:00.000' if no row yet.

    def save(self, lens: str, watermark: str) -> None:
        # INSERT ... ON CONFLICT DO UPDATE SET watermark=..., updated_at=now(), updated_by=...
```

Connection is opened lazily, autocommit, held for the worker's lifetime. Each
save is one UPSERT. Save failures propagate out — the run loop dies, K8s
restarts the pod, and `load()` returns the last successfully-saved watermark.
The dedup token then prevents double-write on re-fetch (see §9.2).

---

## 7. The lenses

### 7.1 Performance — 14 specs

CPU-only. No external services beyond ClickHouse and Postgres.

`build_context` extracts the things multiple specs need:

```python
{
    "started": started_dt,
    "ended": ended_dt,
    "latency_ms": (ended - started).ms,
    "status": span_status.lower(),
    "md": parsed_metadata,
    "usage": md["usage"] if dict else {},
    "ttft_ms": md["first_token_at"] - started,
    "queue_wait_ms": md["scheduled_at"] - md["enqueued_at"],
    "scheduling_delay_ms": started - md["scheduled_at"],
}
```

Then every spec is a one-liner reading off `ctx`. The categorical fields
(`error_type`, `http_status_code`, `degradation_level`) stay in `metadata` —
they have no numeric value and ride along as grouping context.

### 7.2 Cost — 22 specs

CPU-only. Reads pricing from Postgres on a TTL cache (`pricing.py`).

`span_types = ("model_call", "embedding", "tool_call", "retrieval")` is pushed
into the fetch query so non-billable spans never enter the worker.

`build_context` does the pricing math once per span:

- Look up the component's pricing JSONB from the cache
- Apply input_tokens × input_price, output_tokens × output_price
- Add tool/embedding/retrieval flat fees
- Compute waste (failed calls, retried calls) for the wasted_cost metric

Pricing cache refreshes from Postgres every `SIGNAL_PRICING_TTL` seconds. Stale
pricing means a window of metrics computed against old rates; we'd rather show
yesterday's prices than block the worker.

### 7.3 Safety — 4 specs, the complex lens

This is the lens that drives most of the architecture decisions. It loads ML
models, runs batched inference, and handles two independent analysis pipelines
(PII detection via Presidio, content classification via the toxicity package).

#### The pipeline

```
process_batch(spans):
  1. Stage 1 gate    → drop spans with no active safety threshold
  2. For each registered PrefillStep (pii, toxicity):
       a. Sub-filter to spans needing this step
       b. Extract texts (input + output for PII; input only for toxicity)
       c. Dedup by content hash against the LRU cache
       d. Call step.analyze on unique texts
       e. Cache results by content hash
  3. For each kept span:
       ctx = build_context(span)             # reads from caches
       for spec in specs:                    # 4 specs
         emit_if_applies(spec, ctx)
```

#### The lazy model holders

```python
@property
def pii_engine(self):
    if self._pii_engine is None:
        from deidentifier import PresidioEngine
        self._pii_engine = PresidioEngine.get_instance(ner_model=self.ner_model)
    return self._pii_engine

@property
def toxicity_classifier(self):
    if self._toxicity_classifier is None:
        from toxicity_observability import ToxicityClassifier
        self._toxicity_classifier = ToxicityClassifier(config_dict=self._toxicity_config())
    return self._toxicity_classifier
```

The Presidio NER model loads on first PII analysis. The toxicity package loads
its three models (FastText + PI BERT + Moderation BERT) lazily inside its own
`ToxicityClassifier` — so the *imports* happen on first access to `tox`, but
the *model loads* are further deferred to first classify call.

Net result: a Safety pod that only has `pii_count` toggles active never loads
the toxicity models. Saves ~1 GB of RAM per pod in narrow-toggle deployments.

#### The toxicity routing pipeline

Inside `ToxicityClassifier.classify`:

```
normalize → deterministic rules → FastText router
                                        │
                ┌───────────────────────┼───────────────────────┐
                ▼                       ▼                       ▼
          fast_allow              fasttext_direct          run_attack and/or
          (both routes            (one route very          run_moderation
           below threshold,        high → trust FT,         (escalate to BERT)
           skip everything)        skip BERT)
                │                       │                       │
                └───────────────────────┴───────────────────────┘
                                    │
                                    ▼
                            emit {labels, scores, ...}
```

~99% of clean traffic hits `fast_allow` (~1 ms). Only borderline texts pay the
BERT cost. See `toxicity/README.md` for the math.

#### Why two analyzers, one lens

Both PII and toxicity emit *safety* metrics — they're conceptually one lens
("things you'd want to know about content"). Splitting them into two lenses
would mean two Deployments, two checkpoint rows, two batches of the same
spans. Sharing one lens lets the Stage 1 gate run once and the prefill steps
share the same kept-spans list.

### 7.4 Adding a new lens

1. Create `signal_worker/lenses/<name>.py` with a `SpecWorker` subclass:
   - Set `lens` (string), a `SPECS` list of `MetricSpec`, optionally `span_types`.
   - Implement `build_context(span)` to parse everything specs need, once.
2. Register it in `run_worker.py`'s `LENSES` dict.
3. Add a `Dockerfile.<name>` if it needs its own image:
   ```dockerfile
   FROM signal-worker:base
   ENTRYPOINT ["python", "run_worker.py", "--worker", "<name>"]
   ```
4. Add a row to `infra/postgres/init/02_thresholds.sql` and the lens-specific
   threshold table if it needs new toggle dimensions.
5. Validate offline with a CSV (`--csv`), then run `--once` live.

A reference is `lenses/performance.py` (~80 lines: 14 specs declared, 30 lines
of `build_context`).

---

## 8. Deployment architecture

### 8.1 The four images

```
                        python:3.11-slim
                               │
                               ▼
                  signal-worker:base  (~400 MB, internal — not deployed)
                    │           │           │
        ┌───────────┘           │           └───────────┐
        ▼                       ▼                       ▼
 :performance              :cost                     :safety
 (~400 MB)                 (~400 MB)                 (~5.5 GB)
 [framework only]          [framework only]          [+ torch + transformers
                                                     + presidio + spaCy
                                                     + toxicity + PII pkg
                                                     + model weights at /opt/models
                                                     + HF cache at /opt/hf-cache]
```

`:base` is a pure framework image: Python venv, CH/PG client libs, pydantic,
and the `signal_worker` package source. It is not deployed directly — only the
three child images are.

`:performance` and `:cost` differ from `:base` only by the `ENTRYPOINT` —
identical bytes underneath. The image registry deduplicates the shared layers,
so they cost roughly one image's worth of disk on a node, not three.

`:safety` extends `:base` with the heavy ML stack and bakes the four toxicity
model artifacts + the PII NER model into the image. ~5.5 GB total. Pulled once
per node; subsequent Safety pods on the same node start instantly.

### 8.2 Why bake models into the image (not init container + PVC)

Two options were considered:

**(A) Bake into image.** Dockerfile stage 1 downloads weights via
`toxicity-observe download` + the `transformers.pipeline()` pre-cache. ~5.5 GB
image, 0 ms model-fetch on pod start. Chosen.

**(B) Init container + ReadWriteMany PVC.** Slim image (~1 GB). An init pod
downloads to a shared volume; main pod mounts it read-only. Models swappable
without rebuilds. Multiple pods share one PVC.

Tradeoffs:

| | Bake in (chosen) | Init + PVC |
|---|---|---|
| Pod boot time | 0 ms model load (already on disk) | Wait for init container's HF download, or PVC mount |
| Image size | 5.5 GB | ~1 GB |
| Reproducibility | Image SHA pins weights | Image + PVC state must both be tracked |
| HF outage risk | Zero at runtime | First pod waits on HF |
| Model rotation | Rebuild image | Re-run init |

For our scale and team, bake-in is operationally simpler and the SHA pinning
of image-to-weights is a real correctness win. The 5.5 GB cost is paid once
per node and amortized.

If model rotation becomes painful — e.g. weekly classifier retrains — switch
to (B). The Dockerfile change is small.

### 8.3 K8s topology

```
Namespace: signal
  ConfigMap  signal-worker-config       (non-secret env: CH_HOST, PG host, toxicity thresholds, …)
  Secret     signal-worker-secrets      (PG_DSN, CH_PASSWORD)
  Deployment signal-worker-performance  image=:performance, replicas=1
  Deployment signal-worker-cost         image=:cost,        replicas=1
  Deployment signal-worker-safety       image=:safety,      replicas=1
  Service    signal-worker-metrics      port=8080, selects all worker pods, for Prometheus
```

Per Deployment:

- `livenessProbe`: `GET /healthz:8080`
- `readinessProbe`: `GET /readyz:8080`
- `terminationGracePeriodSeconds: 60` — drain time for an in-flight batch
- `resources.requests/limits`:
  - Performance/Cost: ~250m CPU, ~512Mi RAM each
  - Safety: ~1 CPU, ~4Gi RAM (more RAM for model weights)
- `replicas: 1` (see [§11.4](#114-horizontal-scaling-safety))

The Service is purely for Prometheus discovery; workers don't serve traffic.

### 8.4 Image registry strategy

For GCP Artifact Registry (the production target):

```
REGION-docker.pkg.dev/PROJECT/signal/signal-worker:base-v<version>
REGION-docker.pkg.dev/PROJECT/signal/signal-worker:performance-v<version>
REGION-docker.pkg.dev/PROJECT/signal/signal-worker:cost-v<version>
REGION-docker.pkg.dev/PROJECT/signal/signal-worker:safety-v<version>
```

The `:base` image is pushed even though it's not deployed directly — child
images reference its layers, so the registry needs it.

Tagging: use immutable semver tags for releases (`:performance-v1.2.3`) and a
moving `:latest` for the dev cluster.

---

## 9. Data correctness guarantees

This is the section most worth understanding before making changes. The
worker pipeline crosses two stores (CH and PG) with no shared transaction,
talks to a materialized view that fires synchronously on inserts, and is
expected to survive arbitrary crashes. Here's exactly what we guarantee and
what we don't.

### 9.1 Append-only writes

Workers only `INSERT` into `signal_derived_metrics`. They never UPDATE or
DELETE. They never write to `signal_aggregated_metrics` directly — the MV
does that.

This is enforced by code structure (the only insert in the worker is the
`write()` method) and verifiable on the cluster (CH user permissions could
deny UPDATE/DELETE on the table; we don't currently enforce that but probably
should in production).

### 9.2 Idempotency via dedup tokens

Each batch is written with:

```python
insert_deduplication_token = f"{lens}:{newest_recorded_at}:{batch_size}"
```

Deterministic given the same fetch result. ClickHouse remembers the last 1000
tokens per table (`non_replicated_deduplication_window = 1000`) and **drops
duplicate inserts silently — without firing the MV.**

This means:

| Failure mode | What happens |
|---|---|
| Crash between fetch and write | Nothing was written. Re-fetch on restart is harmless. |
| Crash between write and save_checkpoint | Restart re-fetches the same spans, recomputes the same token, CH drops the insert, MV does nothing. ✅ Safe. |
| Pod evicted mid-batch | Same as above. SIGTERM → drain (up to 60s) → if not done, SIGKILL → restart → idempotent replay. ✅ Safe. |
| Two pods of same lens (incorrect deployment) | Both fetch overlapping batches. The later writer's insert is dropped. Wasted compute but no double-count. ✅ Safe-ish (compute is wasted but data is correct). |
| Manual rewind of checkpoint by more than ~1000 batches | Re-fetch produces tokens that have fallen out of the dedup window. ❌ Source table gets duplicate rows; MV fires twice. Aggregated table double-counts. |

The 1000-token window is sized for normal restart scenarios (where the window
between failure and recovery is seconds, not days). For deliberate large
rewinds, use the manual cleanup pattern in `infra/README.md` § "Reset and re-init".

### 9.3 Per-lens isolation

Each lens has:
- Its own checkpoint row in `worker_checkpoints` (independent watermark).
- Its own Deployment (independent failure domain).
- Its own image (independent dependency set).
- Its own toggle cache (different thresholds).

A crash, lag, or replay in Safety does not affect Performance or Cost. This is
load-bearing for production operability: a botched Safety deploy can be rolled
back without touching the other lenses.

### 9.4 What's *not* guaranteed

| Property | Guarantee |
|---|---|
| Exactly-once across CH and PG | ❌ — no shared transaction. Token gives effective once-per-row in CH; PG watermark can technically lag. |
| Strict ordering within a lens | ❌ — each batch is processed atomically, but inter-batch ordering depends on `ORDER BY recorded_at LIMIT N` and the dedup window |
| Span-boundary ties | ❌ — `recorded_at > wm LIMIT N` can skip rows that share the boundary timestamp if a tie spans the batch edge. Mitigated by `WORKER_BATCH = 5000`; a robust fix is `(recorded_at, span_id)` cursor pagination, deferred |
| Multi-pod fan-out for same lens | ❌ — both pods fetch the same spans, wasting compute; the dedup token prevents data corruption but not the waste. Need partitioned consumption (§11.4) |

---

## 10. Observability

Workers self-instrument over HTTP on port 8080 (configurable). Single server,
three endpoints, daemon thread.

### 10.1 The endpoints

| Endpoint | Returns | Used for |
|---|---|---|
| `GET /healthz` | 200 always (while process alive) | K8s liveness — restart pod if this fails |
| `GET /readyz` | 200 once `run_poll` started, else 503 | K8s readiness — gate Service routing during rolling updates |
| `GET /metrics` | Prometheus exposition format | Scrape target |

### 10.2 The metrics

All labeled by `lens`:

| Metric | Type | Meaning |
|---|---|---|
| `signal_worker_batches_total{lens,result}` | counter | result ∈ `success | error | empty` |
| `signal_worker_spans_processed_total{lens}` | counter | spans fetched |
| `signal_worker_rows_emitted_total{lens}` | counter | derived rows written |
| `signal_worker_skipped_at_gate_total{lens}` | counter | spans dropped at Stage 1 |
| `signal_worker_batch_duration_seconds{lens}` | histogram | process_batch wall-clock |
| `signal_worker_write_duration_seconds{lens}` | histogram | CH insert wall-clock |
| `signal_worker_checkpoint_lag_seconds{lens}` | gauge | now − last span's recorded_at |

### 10.3 Why these specifically

Five questions an operator needs to answer:

1. **Is it running?** → `/healthz` + `signal_worker_batches_total{result="success"}` rate.
2. **Is it keeping up?** → `signal_worker_checkpoint_lag_seconds` is the primary scaling input.
3. **Is it healthy?** → `signal_worker_batches_total{result="error"}` rate.
4. **What's it doing?** → `spans_processed`, `rows_emitted`, `skipped_at_gate`.
5. **Is it fast enough?** → `batch_duration_seconds` p95.

`checkpoint_lag_seconds` is the metric for HPAs to scale on — when Safety falls
behind by more than ~5 minutes, the HPA spins more replicas. (Multi-replica
Safety still needs partitioned consumption first; see §11.4. The metric is the
right input regardless.)

### 10.4 Why not push-based metrics

We considered StatsD / OTel push. Pull won because:

- **Prometheus is the default in our K8s cluster** — already running, already
  scraping by pod selector.
- **Pull is more robust** to lossy networks — the scraper retries; the worker
  doesn't have to buffer.
- **Cardinality control** is easier — we don't accidentally emit a metric for
  every span, only per-batch / per-lens counters.

---

## 11. Tradeoffs and deferred work

The most useful section to read before making changes. Each subsection
documents a decision we *didn't* make, why, and what would trigger us to
revisit.

### 11.1 Why one MV at 1-minute, not multiple at multiple grains

We considered N MVs each producing a coarser bucket (30s, 5m, 1h, 1d). Picked
one base grain + read-time merge because:

- **Storage**: one MV ≈ `n × span_count / 60` rows. N MVs ≈ `n × span_count × (1/60 + 1/300 + 1/3600 + 1/86400)`. The 1-min term dominates anyway; coarser MVs add little.
- **MV failure surface**: each MV is a synchronous trigger. More MVs = more places for an insert to fail.
- **Read latency**: merging 60 one-minute buckets for an hourly view costs microseconds; ClickHouse is built for this.

Revisit if: dashboard p99 latency on 1d windows exceeds ~500ms on the
production data volume. Adding a `mv_agg_1d` is a one-time DDL change with a
backfill.

### 11.2 Why dedup tokens, not `ReplacingMergeTree`

Covered in [§4.5](#45-why-no-replacingmergetree-on-derived) and [§9.2](#92-idempotency-via-dedup-tokens).

Short version: ReplacingMergeTree deduplicates at merge time, after the MV
has already fired and double-counted. Dedup tokens prevent the duplicate
insert *and* the MV firing. The right tool for the actual problem.

### 11.3 Why CPU-only images, no GPU yet

Performance/Cost have no ML — CPU is the only option. Safety's models are
small enough (combined ~500M params across FastText + 2× DeBERTa + PII NER)
that CPU is workable: ~120 ms full-scan latency on a single span, ~1 ms on the
fast-allow path that handles 99% of traffic.

A GPU build (`Dockerfile.safety.gpu`) is sketched but not built. CUDA base
images are 8+ GB and complicate pull cost. Revisit if:

- Safety's p95 batch duration exceeds 10s consistently.
- Safety becomes throughput-bound (>50% CPU-saturated in steady state).
- GPU spot capacity in GCP is cheaper than CPU for our load shape.

### 11.4 Horizontal scaling Safety

Today: **1 replica per lens.** If Safety can't keep up, we don't scale it
horizontally yet — two Safety pods would both fetch the same spans and waste
compute (dedup protects correctness, not effort).

The plan when it bites: **partitioned consumption** via `cityHash64(trace_id) %
N`. Each pod owns its hash bucket. The `worker_checkpoints.partition_key`
column is already there for this — each pod will UPSERT to a different row.

A simpler but messier alternative: switch from polling to NATS consumer groups
where the broker handles partitioning. The `on_message` stub exists for this.

Deferred until we measure a real throughput ceiling.

### 11.5 Per-lens images vs single image with --worker arg

Considered single image with `args: ["--worker", "<name>"]` overrides per
Deployment. Rejected because Performance/Cost would pull 5.5 GB they don't
use. Three lens images sharing a `:base` is the smallest-blast-radius shape.

### 11.6 Bake models in vs init-container + PVC

Covered in [§8.2](#82-why-bake-models-into-the-image-not-init-container--pvc).

### 11.7 No streaming ingestion yet

`on_message` is a stub. We're poll-only against ClickHouse. NATS JetStream is
already in the infra compose file (commented out) for the day this changes.
The motivation will be sub-second observability latency (today ~2-5s end-to-
end). Until product asks for it, poll is simpler.

### 11.8 No quality lens yet

Quality (`relevance`, `faithfulness`, `coherence`, `context_relevance`, …)
needs a pluggable judge — typically an LLM call. The PrefillStep abstraction
(§6.5) is built for it: Quality will register a step that batches one judge
call across many spans. The interface for the judge is intentionally not
designed yet — we want to see one production deployment before fixing the
abstraction.

### 11.9 No outcomes lens yet

`outcomes` (task-level success, user-rated satisfaction) needs an "outcome
event" that arrives separately from the spans (e.g. user clicks "thumbs up"
two minutes after the agent runs). The data model accommodates it but we
haven't built the lens.

---

## 12. Entity model glossary

| Term | Meaning |
|---|---|
| **solution** | Top-level product (e.g. "customer support assistant"); everything is scoped under one |
| **endpoint** | Entry point into a solution (`/api/v1/extract`, a cron, a queue listener) |
| **workflow** | Reusable orchestration grouping agents |
| **agent** | Reusable actor; capabilities come from the components bound to it |
| **component** | Unified registry for model / tool / skill / function / knowledgebase / memory (discriminated by `component_type`; `pricing`/`metadata` JSONB carry type-specific fields) |
| **binding** | A *use* row: "in this (solution, endpoint, workflow?, agent?) context, this asset runs with this config". Deepest non-NULL FK = the target |
| **threshold** | Alert limit for a metric at a scope (`category` ∈ performance/quality/cost/safety/outcomes); warning + critical values |
| **scope** | Which entity-path level a metric is *about* (`solution` / `endpoint` / `workflow` / `agent` / `component`) |
| **lens** | A category of metrics + the worker that produces them (today: performance, cost, safety; planned: quality, outcomes) |
| **spec** | One declared metric: `metric, applies, pattern, inputs, unit, window, threshold, per_span, meta_fn` |
| **pattern** | A reusable compute function (`column_latency`, `ratio`, `ctx_value`, …); patterns are shared across lenses |
| **predicate** | A reusable "which spans" filter (`llm_call`, `billable`, `retryable`, …) |
| **PrefillStep** | A batched expensive-analysis step (PII detection, toxicity classification, future LLM-judge calls); registered per lens |
| **toggle** | A `(scope, path, metric)` tuple that has an active threshold in Postgres — used as the gate to decide whether to compute |
| **dedup token** | A deterministic per-batch string passed to ClickHouse `INSERT` so a re-attempted insert is dropped silently |
| **base grain** | The 1-minute bucket the materialized view writes; all coarser windows are computed at read time by merging base buckets |

---

## 13. Future evolution

In rough priority order. Each is independent of the others; we don't have to
do them in this order.

### 13.1 Partitioned consumption — required to multi-pod Safety

Hash-partition by `cityHash64(trace_id) % N`. Each pod owns one partition;
`worker_checkpoints.partition_key` becomes meaningful. Cost: ~100 lines in
`base.py`, a config field, a script to rebalance partitions.

### 13.2 NATS streaming ingestion — sub-second observability

Replace polling with a NATS JetStream consumer group. The `on_message` stub is
the hook. Buys ~2-5s of end-to-end latency and gives partitioning for free
(consumer group). Cost: NATS infra + ~200 lines of consumer code + the
durability story for "what if NATS is down."

### 13.3 Quality lens

The next lens to build. Needs a pluggable judge (an LLM call). The
PrefillStep abstraction is ready for it. Hard parts are the judge interface
design and the cost-control story (judge calls are expensive).

### 13.4 Outcomes lens

Needs an outcome-event stream separate from spans. Probably a separate NATS
subject, joined to the trace's spans by `trace_id` or `correlation_id`.

### 13.5 GPU Safety image

`Dockerfile.safety.gpu` based on `nvidia/cuda:12.4-runtime-ubuntu22.04`. Same
code, different base. Triggered by cost/latency data, not preemptive.

### 13.6 ReplicatedMergeTree for ClickHouse HA

Today the compose stack is single-node CH. For prod, switch to ClickHouse
Cloud or run ReplicatedMergeTree with ClickHouse Keeper. The dedup window
becomes `replicated_deduplication_window` (the equivalent setting on the
replicated engine) and works the same way.

### 13.7 Async batch fetch

Today `fetch_batch` is synchronous; the worker idle-waits during the CH query.
Async fetch overlapped with previous-batch processing would improve throughput
on small batches. Low priority — the bottleneck is compute, not fetch.

---

## Appendix — Reading list

- `README.md` — operational doc (install, configure, deploy, troubleshoot).
- `infra/README.md` — data store schemas and ops.
- `toxicity/README.md` and `PII/README.md` — the two sibling packages used by Safety.
- ClickHouse docs: <https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/replication> for the dedup window mechanics.
- Presidio: <https://microsoft.github.io/presidio/> for PII detection internals.
- The original "spec model" inspiration is loose, but the closest analog in
  industry is OTel's "metric definition" concept — declarative, library-of-
  patterns-style.