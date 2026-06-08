# Signal Workers

Workers read immutable spans from ClickHouse `signal_raw_spans`, compute per-lens
metrics, and append them to `signal_derived_metrics`. A materialized view then
rolls those into `signal_aggregated_metrics` automatically — **workers never touch
the aggregated table.**

```
signal_raw_spans ──► [lens worker] ──► signal_derived_metrics ──(MV)──► signal_aggregated_metrics
   (what happened)     (compute)          (one row / metric / span)        (1-min rollup buckets)
```

**One worker per lens.** Each lens is its own class and runs as its own process
with its own checkpoint, so they scale and deploy independently. Built today:
`performance` and `cost`. Planned: `quality`, `safety`, `outcomes`.

> For *why* it's built this way — the data flow, the aggregation internals, the
> caching, and the production-readiness notes — see **ARCHITECTURE.md**.

---

## 1. Layout

```
signal-workers/
├── run_worker.py            CLI entrypoint — launches one or all lenses
├── show_specs.py            print a lens's metric registry as a table
├── validate_performance.py  offline correctness check vs the mock derived data
├── requirements.txt
├── README.md                (this file)
├── ARCHITECTURE.md          technical design + full flow
└── signal_worker/
    ├── config.py            env-driven CH/PG connection + run-loop params
    ├── base.py              the engine-agnostic loop: fetch ► compute ► write ► checkpoint
    ├── spec.py              MetricSpec + SpecWorker (the spec-driven engine)
    ├── patterns.py          reusable compute patterns (column_latency, ratio, ctx_value, …)
    ├── predicates.py        reusable "which spans does this apply to" filters
    ├── pricing.py           PricingCache — pulls component prices from Postgres, caches them
    └── lenses/
        ├── performance.py   Performance lens — 17 metrics
        └── cost.py          Cost lens — 22 metrics
```

A lens is tiny: it declares its `specs` and a `build_context(span)`; the base
class does batching, the watermark, writes, and restart-safe checkpointing.

---

## 2. Prerequisites

- **Python 3.9+**
- **ClickHouse** reachable over its HTTP port (default `8123`), with the Signal
  schema loaded (`signal_raw_spans`, `signal_derived_metrics`,
  `signal_aggregated_metrics`, and the `mv_agg_base` materialized view). See the
  `infra/` package.
- **Postgres** reachable (default `5432`) with the `components` table seeded —
  **only the Cost lens (and later lenses) need this**; Performance does not.

The workers run **on the host** and connect to ClickHouse/Postgres over their
published ports (e.g. `localhost:8123`, `localhost:5432`). Container names are
irrelevant to the workers — only the host:port matters.

---

## 3. Install

```bash
pip install -r requirements.txt
```

That installs `clickhouse-connect` (live mode) and `psycopg[binary]` (used by the
Cost lens to read prices from Postgres). For **offline testing only**, you don't
strictly need either — the `--csv` path uses no database.

---

## 4. Configuration (environment variables)

All configuration is environment-driven with local-friendly defaults
(`signal_worker/config.py`). Set these when running against your stack:

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `CH_HOST` | `localhost` | all | ClickHouse host |
| `CH_PORT` | `8123` | all | ClickHouse **HTTP** port |
| `CH_DB` | `signal` | all | ClickHouse database |
| `CH_USER` | `default` | all | ClickHouse user |
| `CH_PASSWORD` | `` (empty) | all | ClickHouse password |
| `PG_DSN` | `postgresql://postgres@localhost:5432/signal` | cost + later | Postgres DSN for pricing/config |
| `WORKER_BATCH` | `5000` | all | spans fetched per batch |
| `WORKER_POLL_SEC` | `2.0` | all | sleep between polls (continuous mode) |
| `WORKER_STATE_DIR` | `/var/lib/signal-workers` | all | where watermark files are stored |

**On Windows/PowerShell**, set the state dir somewhere writable, e.g.:

```powershell
$env:CH_HOST="localhost"; $env:CH_PORT="8123"; $env:CH_DB="signal"
$env:CH_USER="default";   $env:CH_PASSWORD=""
$env:PG_DSN="postgresql://postgres:postgres@localhost:5432/signal"
$env:WORKER_STATE_DIR="$PWD\state"
```

On bash:

```bash
export CH_HOST=localhost CH_PORT=8123 CH_DB=signal CH_USER=default CH_PASSWORD=
export PG_DSN="postgresql://postgres:postgres@localhost:5432/signal"
export WORKER_STATE_DIR="$PWD/state"
```

---

## 5. Run (live, against ClickHouse/Postgres)

```bash
python run_worker.py --worker all                # run all lenses concurrently
python run_worker.py --worker performance        # continuous poll loop, one lens
python run_worker.py --worker performance --once # process one batch, then exit
python run_worker.py --worker cost --once        # Cost lens (reads prices from Postgres)
python run_worker.py --worker all --once         # one batch each, all lenses, then exit
```

Each lens runs in its own thread (when using `--worker all`) or as the main
process (single lens). The worker pulls spans with `recorded_at > watermark`,
computes metrics, inserts into `signal_derived_metrics`, and advances its watermark.

**Each lens has its own watermark**, so they run fully independently:

```
state/performance.watermark
state/cost.watermark
```

On first run a lens has no watermark, so it **backfills the entire span history**
from the beginning — exactly once, independently per lens.

### Re-running / resetting a lens

Delete that lens's watermark to make it reprocess from scratch:

```powershell
Remove-Item .\state\cost.watermark -ErrorAction SilentlyContinue
python run_worker.py --worker cost --once
```

> ⚠️ Reprocessing **appends** to `signal_derived_metrics` (it's an append-only
> table). To avoid double-counting on a full re-run, truncate the derived +
> aggregated tables first (see ARCHITECTURE.md → "Idempotency").

### Tuning throughput

For a one-shot backfill, a bigger batch means a single fetch + single insert:

```powershell
$env:WORKER_BATCH="20000"
python run_worker.py --worker performance --once
```

---

## 6. Test offline (no ClickHouse, no Postgres)

The **same** `compute()` logic runs over an exported spans CSV — this is how each
lens is validated.

```bash
# write a lens's output to a CSV
python run_worker.py --worker performance --csv signal_raw_spans.csv --out perf_derived.csv
python run_worker.py --worker cost        --csv signal_raw_spans.csv --out cost_derived.csv
```

The Cost lens in `--csv` mode uses the known seed prices (no Postgres needed).

### Correctness check

`validate_performance.py` diffs the worker's output against the reference (mock)
derived data, per metric:

```bash
python validate_performance.py signal_raw_spans.csv signal_derived_metrics.csv
```

Spot-check counts per metric from any derived CSV (Python one-liner):

```bash
python -c "import csv,collections;c=collections.Counter(r['metric'] for r in csv.DictReader(open('perf_derived.csv')));[print(f'{m:24}{c[m]}') for m in sorted(c)]"
```

---

## 7. Inspect a lens's metric registry

```bash
python show_specs.py performance
python show_specs.py cost
```

Prints every metric the lens owns: which spans it applies to, the compute
pattern, unit, rollup window, whether it has a threshold, and whether it's
per-span or computed at read time.

---

## 8. Verify results in ClickHouse

Per-metric row counts the worker wrote:

```sql
SELECT metric, count()
FROM signal_derived_metrics
GROUP BY metric ORDER BY metric;
```

Reading the **aggregated** table requires merging the aggregate-function states
(selecting them raw prints binary garbage like the replacement character). Always
use the `-Merge` combinators with a `GROUP BY`:

```sql
-- current p95 latency for a solution over the last 3h
SELECT
    quantilesTDigestMerge(0.95)(quantiles) AS p95,
    avgMerge(avg_value)                    AS avg,
    sum(count)                             AS n
FROM signal_aggregated_metrics
WHERE solution_id = 'sol_support'
  AND metric = 'latency'
  AND scope  = 'solution'
  AND ts > now() - INTERVAL 3 HOUR;
```

Total cost rolled up by solution (cost is additive — sum the component-leaf rows):

```sql
SELECT solution_id, sum(sum_value) AS total_cost
FROM signal_aggregated_metrics
WHERE metric = 'cost' AND component_id != ''
GROUP BY solution_id;
```

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `count() = 0` everywhere | spans CSV not loaded into ClickHouse | load `signal_raw_spans` (see `infra/` load script) |
| Reading aggregated shows garbage chars | selecting aggregate-function states raw | use `*Merge` combinators + `GROUP BY` (§8) |
| Cost worker slow / hammering Postgres | stale build | ensure `pricing.py` serves from cache + Cost declares `span_types` |
| Duplicate/inflated numbers after re-run | reprocessed spans appended again | truncate derived + aggregated before a full re-run |
| `psycopg` import error on Cost | dependency missing | `pip install "psycopg[binary]"` |
| Watermark won't advance | spans share identical `recorded_at` at the batch edge | increase `WORKER_BATCH` so the batch clears the tie |

---

## 10. Add a new lens (quick version)

1. Create `signal_worker/lenses/<lens>.py` with a `SpecWorker` subclass:
   set `lens`, a `SPECS` list of `MetricSpec`, optionally `span_types`, and a
   `build_context(span)` that parses everything the patterns need **once**.
2. Register it in `run_worker.py` and `show_specs.py` (`LENSES` dict).
3. Validate offline with `--csv`, then run `--once` live.

See ARCHITECTURE.md → "Adding a lens" for the full walkthrough.
