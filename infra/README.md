# Signal Infra

Brings up the two datastores, applies all schema + seed config on first boot,
and exposes the ports the workers (and later the API) connect to.

| Service | Container | Purpose | Ports |
|---|---|---|---|
| postgres | `signal-postgres` | config / registry / thresholds | 5432 |
| clickhouse | `signal-clickhouse` | telemetry (spans, metrics) | 8123 (HTTP), 9000 |

## Layout

```
infra/
├── docker-compose.yml
├── .env.example                 # copy to .env to override creds/ports
├── postgres/init/               # applied alphabetically on FIRST boot
│   ├── 00_schema.sql            # v5 schema: enums, 7 tables, indexes, scope CHECK
│   ├── 01_registry_bindings.sql # solutions/endpoints/workflows/agents/components + bindings
│   └── 02_thresholds.sql        # 721 thresholds
├── clickhouse/init/
│   └── 00_schema.sql            # raw_spans + derived_metrics + aggregated + MV
├── scripts/
│   ├── load_clickhouse.ps1      # load the telemetry CSVs (data not in init)
│   └── verify.ps1               # row counts across both stores
└── data/                        # drop the two CSVs here before loading
```

## How init works

Both images run any `*.sql` in `/docker-entrypoint-initdb.d` **on first boot only**
(i.e. when the data volume is empty), in alphabetical order. So:

* Postgres applies `00_schema` → `01_registry_bindings` → `02_thresholds` automatically.
* ClickHouse applies the DDL, which self-creates the `signal` database and the MV.

The big telemetry CSVs are **not** init scripts (too large) — load them after the
stack is up via the script below.

## Run

```powershell
# 1. (optional) cp .env.example .env  and edit
# 2. bring up both stores — Postgres comes up fully schema'd + seeded
docker compose up -d

# 3. wait for health, then load telemetry (after placing CSVs in .\data\)
.\scripts\load_clickhouse.ps1

# 4. sanity check
.\scripts\verify.ps1
```

Expected after load: Postgres → 2 solutions / 4 endpoints / 8 agents / 8 components
/ 36 bindings / 721 thresholds. ClickHouse → raw 17,346 / derived 705,435 /
aggregated (smaller, MV-rolled).

Then run the workers (separate `signal-workers` package) on the host, pointed at
`localhost:8123`.

## Notes

* **Container names** are `signal-postgres` and `signal-clickhouse`. If you were
  using `infra-postgres-1` before, update any `docker exec` commands accordingly.
* **Reset from scratch:** `docker compose down -v` removes the volumes, so the next
  `up` re-runs all init scripts.
* **NATS** is only needed when you switch workers from poll mode to the streaming
  path — left commented in the compose file.
