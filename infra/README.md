# Compass Infra — Postgres + ClickHouse

Brings up the two datastores the Compass platform runs on:

- **Postgres** — registry (solutions, endpoints, workflows, agents, components,
  bindings), thresholds, and worker checkpoints. The "what exists / what should
  be" store.
- **ClickHouse** — telemetry (raw spans, derived metrics, aggregated rollups).
  The "what happened" store.

Applies all schema + seed config on first boot, exposes the ports the workers
(and later the API) connect to.

| Service | Container | Purpose | Host ports |
|---|---|---|---|
| postgres | `compass-postgres` | Registry, bindings, thresholds, worker_checkpoints | `5432` |
| clickhouse | `compass-clickhouse` | Raw spans, derived metrics, aggregated metrics | `8123` (HTTP), `9000` (native) |

---

## Table of contents

1. [Layout](#1-layout)
2. [Prerequisites](#2-prerequisites)
3. [Quick start](#3-quick-start)
4. [How init works](#4-how-init-works)
5. [Postgres schema](#5-postgres-schema)
6. [ClickHouse schema](#6-clickhouse-schema)
7. [Loading telemetry data](#7-loading-telemetry-data)
8. [Verification](#8-verification)
9. [Connecting from workers](#9-connecting-from-workers)
10. [Reset and re-init](#10-reset-and-re-init)
11. [Production deployment notes](#11-production-deployment-notes)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Layout

```
infra/
├── docker-compose.yml
├── .env.example                       copy to .env to override creds/ports
├── README.md                          (this file)
├── postgres/
│   └── init/                          run alphabetically on FIRST boot
│       ├── 00_schema.sql              v5 schema: enums, 7 registry tables, indexes
│       ├── 01_registry_bindings.sql   seeded solutions/endpoints/workflows/agents/components + bindings
│       ├── 02_thresholds.sql          721 seeded thresholds (perf/quality/cost/safety/outcomes)
│       └── 03_worker_checkpoints.sql  worker high-checkpoint table (lens, partition_key, checkpoint)
├── clickhouse/
│   └── init/
│       └── 00_schema.sql              raw_spans + derived_metrics + aggregated + mv_agg_base MV
├── scripts/
│   ├── load_clickhouse.ps1            load span/derived CSVs into CH (data not in init)
│   └── verify.ps1                     row counts across both stores
└── data/                              drop the CSVs here before loading (gitignored)
    ├── compass_raw_spans.csv
    └── compass_derived_metrics.csv
```

---

## 2. Prerequisites

- **Docker** with Compose v2 (`docker compose` not `docker-compose`).
- ~5 GB free disk for the two volumes.
- Ports `5432`, `8123`, `9000` available on the host (override in `.env` if not).

That's it for the infra stack itself. To actually flow data through it you'll
also need the `compass-workers` package on the host (or in containers).

---

## 3. Quick start

```powershell
cd infra
copy .env.example .env       # optional — defaults work for local dev
notepad .env                 # edit creds/ports if you want
docker compose up -d

# wait for both healthchecks to come up green
docker compose ps

# place CSVs in .\data\ first, then load telemetry
.\scripts\load_clickhouse.ps1

# sanity check
.\scripts\verify.ps1
```

After a clean boot + load, you should see:

```
== Postgres (config) ==
solutions:    2
endpoints:    4
workflows:    2
agents:       8
components:   8
bindings:    36
thresholds: 721

== ClickHouse (telemetry) ==
raw:        17346
derived:   705435
aggregated:  (smaller — MV-rolled per-minute buckets)
```

---

## 4. How init works

Both images run any `*.sql` they find under `/docker-entrypoint-initdb.d/` —
**but only on first boot** (when the data volume is empty). They're applied in
filename order. So:

- **Postgres** runs `00_schema` → `01_registry_bindings` → `02_thresholds` → `03_worker_checkpoints`.
- **ClickHouse** runs `00_schema` which creates the `compass` database, the three
  tables, and the `mv_agg_base` materialized view.

The large telemetry CSVs are **not** init scripts (too big, ~100MB+). Load them
after the stack is up via `load_clickhouse.ps1`.

If you add new init files to either dir after the first boot, they will NOT run
automatically. Either:
- `docker compose down -v` and `up -d` again (re-runs everything), or
- Apply manually via `psql` / `clickhouse-client` (see S10).

---

## 5. Postgres schema

### 5.1 Registry tables (`00_schema.sql`)

The "v5" entity model:

| Table | Purpose | Key columns |
|---|---|---|
| `solutions` | Top-level product | `id`, `solution_id` (slug) |
| `endpoints` | Entry point into a solution (`/api/v1/...`, cron, queue) | `id`, `endpoint_id`, `solution_id`, `method` |
| `workflows` | Reusable orchestration (groups agents) | `id`, `workflow_id`, `name` |
| `agents` | Reusable actor; capability comes from bound components | `id`, `agent_id`, `name` |
| `components` | Unified registry of model/tool/skill/function/knowledgebase/memory | `id`, `component_id`, `component_type`, `pricing` (JSONB), `metadata` (JSONB) |
| `bindings` | "In this context, this asset runs with this config" — deepest non-null FK is the target | `solution_id`, `endpoint_id`, `workflow_id?`, `agent_id?`, `component_id?`, `config` |
| `thresholds` | Alert limits per metric per scope | `metric`, `scope`, `category`, `warning_value`, `critical_value`, materialized-path FKs |

Three enums:

- `component_type_enum`: `model | tool | skill | function | knowledgebase | memory`
- `threshold_category_enum`: `performance | quality | cost | safety | outcomes`
- `scope_level_enum`: `solution | endpoint | workflow | agent | component`

### 5.2 Seeded data (`01_registry_bindings.sql`, `02_thresholds.sql`)

Two solutions, four endpoints, eight agents, eight components, 36 bindings, 721
thresholds. Enough to exercise every lens and every threshold path.

Use the seeded data for development / CI. For production, replace these files
with your actual entity registry before the first boot.

### 5.3 Worker checkpoints (`03_worker_checkpoints.sql`)

One row per `(lens, partition_key)`. Each worker UPSERTs its high-checkpoint
after every batch:

```sql
CREATE TABLE worker_checkpoints (
    lens          TEXT        NOT NULL,
    partition_key TEXT        NOT NULL DEFAULT 'default',
    checkpoint     TEXT        NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS.mmm'
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT,                          -- HOSTNAME of the writer pod
    PRIMARY KEY (lens, partition_key)
);
```

`partition_key` is reserved for future hash-partitioned consumption; always
`'default'` today.

---

## 6. ClickHouse schema

### 6.1 Three tables, one materialized view

```
compass_raw_spans          (MergeTree)                  — immutable span tree
        │
        ▼
compass_derived_metrics    (MergeTree + dedup window)   — workers write here
        │
        ▼  (mv_agg_base fires on every insert)
compass_aggregated_metrics (AggregatingMergeTree)       — 1-min rollup buckets
```

| Table | Engine | Partition | TTL | Notes |
|---|---|---|---|---|
| `compass_raw_spans` | MergeTree | `toYYYYMM(started_at)` (monthly) | 90 days | Read-only for workers |
| `compass_derived_metrics` | MergeTree | `toYYYYMMDD(ts)` (daily) | 90 days | `non_replicated_deduplication_window = 1000` for idempotent writes |
| `compass_aggregated_metrics` | AggregatingMergeTree | `toYYYYMM(ts)` (monthly) | 365 days | Stores aggregate-function states; read with `*Merge` combinators |

### 6.2 The dedup window

The `non_replicated_deduplication_window = 1000` setting on `compass_derived_metrics`
is what makes worker writes idempotent: a re-attempted insert with a previously-
seen `insert_deduplication_token` is dropped silently, and the materialized view
does not fire. See `compass-workers/README.md` S "Data correctness guarantees".

To verify the setting is present:

```bash
docker exec -it compass-clickhouse clickhouse-client --database compass \
    --query "SHOW CREATE TABLE compass_derived_metrics" \
    | grep deduplication
```

You should see `non_replicated_deduplication_window = 1000` in the output.

If you find it missing on an existing deployment, apply it without rebuilding:

```bash
docker exec -i compass-clickhouse clickhouse-client --database compass \
    --query "ALTER TABLE compass_derived_metrics MODIFY SETTING non_replicated_deduplication_window = 1000"
```

### 6.3 The materialized view

`mv_agg_base` rolls every insert into `compass_derived_metrics` into 1-minute
buckets in `compass_aggregated_metrics`. Coarser windows (5m, 1h, 1d) are
**not separate tables** — they're computed at read time by merging base
buckets:

```sql
-- 1h p95 latency
SELECT toStartOfHour(ts) AS hour,
       quantilesTDigestMerge(0.95)(quantiles) AS p95
FROM compass_aggregated_metrics
WHERE metric = 'latency' AND solution_id = 'sol_support'
GROUP BY hour;
```

> **Gotcha:** selecting an aggregate-function state raw returns binary garbage
> (it's a t-digest sketch / avgState struct, not a number). Always use the
> matching `*Merge` combinator with a `GROUP BY`.

### 6.4 Tuning the base grain

To support sub-minute live tailing, edit the MV's `INTERVAL 1 MINUTE` to
`30 SECOND` (in two places inside `00_schema.sql`) and re-init. Higher granularity
costs storage roughly linearly.

---

## 7. Loading telemetry data

After the stack is up, place the two CSVs in `.\data\` and run:

```powershell
.\scripts\load_clickhouse.ps1
```

What it does:

1. `docker cp` both CSVs into `/tmp/` inside the CH container.
2. `INSERT INTO compass_raw_spans FORMAT CSVWithNames` from `/tmp/raw.csv`.
3. `INSERT INTO compass_derived_metrics FORMAT CSVWithNames` from `/tmp/derived.csv` — this **also** fires the MV, filling `compass_aggregated_metrics` automatically.

If you load `compass_derived_metrics` **before** the MV exists (e.g. you create
the MV later), do a one-time backfill:

```sql
-- See infra/clickhouse/init/00_schema.sql "OPTIONAL BACKFILL" section
INSERT INTO compass_aggregated_metrics
SELECT
    scope, solution_id, endpoint, workflow_id, agent_id, component_id,
    component_type, environment, metric, ts,
    count(), sum(value), min(value), max(value),
    avgState(value),
    quantilesTDigestState(0.5, 0.95, 0.99)(value),
    avgState(confidence)
FROM (
    SELECT scope, solution_id, endpoint, workflow_id, agent_id, component_id,
           component_type, environment, metric, value, confidence,
           toStartOfInterval(ts, INTERVAL 1 MINUTE) AS ts
    FROM compass_derived_metrics
)
GROUP BY scope, solution_id, endpoint, workflow_id, agent_id, component_id,
         component_type, environment, metric, ts;
```

---

## 8. Verification

```powershell
.\scripts\verify.ps1
```

Prints row counts across both stores. Expected after a fresh init + load:

```
== Postgres (config) ==
   t           |  count
---------------+-------
 solutions     |     2
 endpoints     |     4
 workflows     |     2
 agents        |     8
 components    |     8
 bindings      |    36
 thresholds    |   721

== ClickHouse (telemetry) ==
   t          |    c
--------------+--------
 raw          |  17346
 derived      | 705435
 aggregated   |  (varies, MV-rolled)
```

A few more spot-checks worth running by hand:

```sql
-- Postgres: confirm worker_checkpoints exists and is initially empty
SELECT count(*) FROM worker_checkpoints;
-- expect: 0 (until workers start writing)

-- ClickHouse: confirm the dedup setting is on
SHOW CREATE TABLE compass_derived_metrics;
-- look for: SETTINGS ... non_replicated_deduplication_window = 1000
```

---

## 9. Connecting from workers

### 9.1 Host mode

Workers running on the host connect to `localhost:8123` (CH) and `localhost:5432`
(PG). Default ports in `.env.example`:

```bash
# compass-workers/.env  (or similar)
CH_HOST=localhost
CH_PORT=8123
PG_DSN=postgresql://postgres:postgres@localhost:5432/compass
```

### 9.2 Container mode (Docker Desktop)

Worker containers running on the same host need to reach the infra containers
via the Docker bridge. Use `host.docker.internal`:

```bash
# compass-workers/.env.docker
CH_HOST=host.docker.internal
PG_DSN=postgresql://postgres:postgres@host.docker.internal:5432/compass
```

(On Linux without Docker Desktop, use `--add-host=host.docker.internal:host-gateway`
or put the workers on the same compose network as infra.)

### 9.3 K8s

Both stores typically live elsewhere in production (managed Postgres, managed
ClickHouse, or a separate cluster namespace). Configure via Secret + ConfigMap:

```yaml
env:
  - name: CH_HOST
    valueFrom: { configMapKeyRef: { name: compass-worker-config, key: CH_HOST } }
  - name: PG_DSN
    valueFrom: { secretKeyRef: { name: compass-worker-secrets, key: PG_DSN } }
```

See `compass-workers/README.md` S "Production deployment".

---

## 10. Reset and re-init

### Full reset

```powershell
docker compose down -v       # -v drops the volumes — DESTRUCTIVE
docker compose up -d
.\scripts\load_clickhouse.ps1
```

This re-runs every init script from scratch.

### Just one schema file

To apply a single SQL file to a running stack without losing other data:

```powershell
# Postgres
docker exec -i compass-postgres psql -U postgres -d compass < postgres/init/03_worker_checkpoints.sql

# ClickHouse
docker exec -i compass-clickhouse clickhouse-client --database compass < clickhouse/init/some_alter.sql
```

### Drop and recreate one table

```sql
-- ClickHouse: see infra/clickhouse/init/00_schema.sql S0b for a copy-pasteable
-- "DROP THESE" block. The MV must be dropped before the tables it reads/writes.
DROP VIEW  IF EXISTS mv_agg_base;
DROP TABLE IF EXISTS compass_aggregated_metrics;
DROP TABLE IF EXISTS compass_derived_metrics;
DROP TABLE IF EXISTS compass_raw_spans;
```

Then re-run the init file:

```powershell
docker exec -i compass-clickhouse clickhouse-client --database compass < clickhouse/init/00_schema.sql
```

---

## 11. Production deployment notes

This compose stack is **for development**. For production:

- **Don't run Postgres or ClickHouse as Docker containers with bind mounts.**
  Use managed services (Cloud SQL, ClickHouse Cloud, Aiven) or proper
  StatefulSets with PVs.
- **Don't ship the seed data.** `01_registry_bindings.sql` and `02_thresholds.sql`
  contain test fixtures; production environments should populate the registry
  via your own onboarding flow.
- **Apply `03_worker_checkpoints.sql` to your production Postgres** before
  deploying any workers. It's idempotent (`CREATE TABLE IF NOT EXISTS`).
- **Verify `non_replicated_deduplication_window` is set on production
  ClickHouse**. The init script includes it, but if you provisioned the table
  before that setting landed, ALTER it on (see S 6.2).
- **Materialized view consistency**: if your prod CH cluster goes through a
  reshard or table-rebuild, the MV must be recreated and old data backfilled
  (see S 7). Plan for an MV outage during such operations.
- **Backups**: Postgres = standard `pg_dump`. ClickHouse = `BACKUP TABLE ... TO
  Disk('backup_disk', '...')`, or use the cloud provider's snapshot story.
- **Retention**: tables have explicit `TTL` clauses (90 days for raw + derived,
  365 days for aggregated). Adjust before going live if your compliance window
  differs.

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker compose up -d` exits but `ps` shows postgres restarting | Init script syntax error | `docker compose logs postgres` — find the SQL line; init runs in alphabetical order, so 00 must succeed before 01 runs |
| Workers can't connect: `Connection refused` to `localhost:8123` | CH container not up yet, or wrong port in `.env` | `docker compose ps`, check healthcheck; verify `CH_PORT=8123` matches the host port mapping |
| `verify.ps1` shows aggregated = 0 after data load | Either MV didn't exist when derived was loaded, OR you're querying before MV catches up | Re-run the backfill INSERT from S7; the MV is async on huge inserts but converges within seconds |
| ClickHouse out of memory on a big load | CH default memory limit | Pass `--max_memory_usage_for_user=8000000000` to clickhouse-client, or split the CSV |
| Init scripts not re-running after edit | Volumes persist between `up`/`down`; init only runs on a clean volume | `docker compose down -v && docker compose up -d` (DESTRUCTIVE), or apply the edited file manually via `docker exec` |
| `permission denied` on `init/` files inside container | File mode issue on the bind mount | `chmod 644 postgres/init/*.sql clickhouse/init/*.sql` and `up -d` again |
| `compass-postgres` container name conflict | Previous deployment with same name | `docker rm -f compass-postgres compass-clickhouse` then `up -d` |
| `SHOW CREATE TABLE` doesn't show the dedup setting | ALTER never applied on this deployment | See S 6.2 |
| Postgres `role "compass" does not exist` from workers | `PG_DSN` user doesn't match the seeded user (which is `postgres`) | Either change DSN to `postgresql://postgres:postgres@host:5432/compass`, or `CREATE USER compass WITH PASSWORD '...'` and grant privileges |

---

## Appendix — Related

- **compass-workers/README.md** — the workers that read from these stores.
- **compass-workers/ARCHITECTURE.md** — why the schema is shaped this way.
- **toxicity/** and **PII/** — sibling packages used by the Safety lens worker.