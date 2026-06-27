# Reconciler — Architecture

Auto-discovers entities, records evidence, seeds threshold rows. Read-only on ClickHouse; write-only on Postgres. Returns `[]` from `process_batch` — emits nothing to `compass_derived_metrics`.

## 1. Position in the system

```mermaid
flowchart LR
  ch[("compass_raw_spans")] -->|poll all span_types| recon["Reconciler<br/>lens='reconciler'<br/>span_types=None"]
  recon -->|"UPSERT solutions/endpoints/<br/>workflows/agents/components"| reg[("PG registry")]
  recon -->|"UPSERT bindings"| bind[("PG bindings")]
  recon -->|"UPSERT observed_span_types<br/>(propagated up the path)"| obs[("PG observed_span_types")]
  recon -->|"INSERT thresholds<br/>is_active=false, values NULL"| th[("PG thresholds")]
  recon -->|"UPSERT worker_checkpoints<br/>(lens='reconciler', pk='default')"| ck[("PG worker_checkpoints")]
  catalog["lens SPECS<br/>(threshold=True)"] -->|"startup upsert"| mc[("PG metric_catalog")]
  recon -.reads.-> mc

  classDef new fill:#fef3c7,stroke:#d97706
  class recon,obs,mc new
```

Yellow = new in coverage feature. Other PG tables existed before.

## 2. Startup sequence

```mermaid
sequenceDiagram
  participant rp as run_poll (override)
  participant ec as _ensure_catalog
  participant pg as PG
  rp->>ec: at process start, before any batch
  ec->>pg: INSERT metric_catalog<br/>(from lens SPECS, threshold=True)<br/>ON CONFLICT UPDATE
  ec->>pg: SELECT * FROM metric_catalog<br/>(cast applicable_scopes::text[])
  ec->>pg: SELECT id, slug FROM solutions<br/>(and 4 more registry tables)
  Note over ec: slug→UUID cache populated<br/>per entity_type
  ec->>pg: _backfill_thresholds(pg)
  Note over ec: For every (cached entity, metric in catalog,<br/>scope ∈ applicable_scopes):<br/>INSERT...SELECT ... WHERE NOT EXISTS<br/>(handles registry entities present before<br/>reconciler ever ran — e.g. dev seed)
  ec->>pg: pg.commit()
  rp->>rp: BaseWorker.run_poll()
```

`metric_catalog` upsert is **idempotent** — `ON CONFLICT (metric) DO UPDATE` refreshes every column. Re-deploy with a new metric and the next pod start picks it up.

`load_metric_catalog` casts `lens::text` and `applicable_scopes::text[]` in the SELECT — psycopg has no default adapter for custom-enum arrays. Defensive `isinstance(..., list)` assertion fails loud if that cast is ever removed.

## 3. Per-batch flow

```mermaid
flowchart TD
  fetch["fetch_batch — all span_types,<br/>partition_key='default'"] --> n{spans?}
  n -->|0| done["return []"]
  n -->|N| tx["BEGIN transaction"]
  tx --> p1["Phase 1 — UPSERT registry<br/>(cache-miss only)"]
  p1 --> p2["Phase 2 — UPSERT bindings<br/>(distinct materialized paths)"]
  p2 --> p3["Phase 3 — UPSERT observed_span_types<br/>(aggregated within batch,<br/>propagated up the path)"]
  p3 --> p4["Phase 4 — INSERT thresholds<br/>(for every entity in batch,<br/>NOT EXISTS guards dedup)"]
  p4 --> commit["pg.commit()"]
  commit --> cache_merge["Merge pending_cache_adds<br/>into slug_cache;<br/>bump NEW_ENTITIES counter"]
  cache_merge --> done2["return []"]

  tx -.failure.-> rollback["pg.rollback()<br/>cache untouched<br/>raise → pod restart"]
```

Single PG transaction per batch. Cache only updates **after** commit succeeds; rollback leaves it untouched so the next batch retries cleanly.

### Phase 1 — registry UPSERTs (cache-miss only)

```mermaid
flowchart LR
  batch["batch of N spans"] --> distinct["distinct slugs per level<br/>(in Python, not SQL)"]
  distinct --> check{"slug in cache?"}
  check -->|hit| reuse["use cached UUID,<br/>0 PG round-trips"]
  check -->|miss| upsert["INSERT ... ON CONFLICT ... RETURNING id<br/>stage UUID in pending_cache_adds"]
  upsert --> ret["slug_to_uuid[(type, slug)] = uuid"]
  reuse --> ret
```

Order is fixed because of FKs: solutions → endpoints (FK on solutions) → workflows → agents → components. Endpoints also set `path = endpoint_id` so the ToggleCache's `e.path` lookup finds the same string spans carry.

### Phase 2 — bindings

One distinct row per `(solution_id, endpoint_id, workflow_id, agent_id, component_id)` 5-tuple seen in the batch. UNIQUE index with `NULLS NOT DISTINCT` makes ON CONFLICT idempotent.

### Phase 3 — observed_span_types

```mermaid
flowchart TD
  span["1 span<br/>span_type=model_call<br/>full path"] --> rec_comp["UPSERT (component, comp_uuid, model_call)"]
  span --> rec_ag["UPSERT (agent, ag_uuid, model_call)"]
  span --> rec_wf["UPSERT (workflow, wf_uuid, model_call)"]
  span --> rec_ep["UPSERT (endpoint, ep_uuid, model_call)"]
  span --> rec_sol["UPSERT (solution, sol_uuid, model_call)"]
```

Propagation makes the Phase 2 coverage view a single join — no recursive walk. Within one batch, all rows for the same `(entity, span_type)` are aggregated in Python first → one UPSERT per distinct key (5,000 spans → ~10-30 UPSERTs).

`sample_count` increments by the per-batch count; `last_seen = NOW()` always; `first_seen = NOW()` on INSERT only.

### Phase 4 — threshold seeding

```mermaid
flowchart TD
  cat["metric_catalog<br/>22 rows, each with applicable_scopes"] --> per_metric["for each metric:<br/>for each applicable scope:"]
  per_metric --> sol_branch{scope = solution?}
  sol_branch -->|yes| sol_sql["INSERT INTO thresholds<br/>(solution_id only,<br/>warning/critical NULL::float8)<br/>SELECT ... FROM solutions<br/>WHERE id = ANY(targets)<br/>AND NOT EXISTS (matching row)"]
  sol_branch -->|no| nonsol_sql["INSERT INTO thresholds<br/>(full path from bindings:<br/>solution_id + endpoint_id + workflow_id<br/>+ agent_id + component_id as applicable)<br/>SELECT DISTINCT ... FROM bindings b<br/>WHERE b.{fk} = ANY(targets)<br/>AND NOT EXISTS<br/>(IS NOT DISTINCT FROM for NULL safety)"]
  sol_sql --> rowcount["THRESHOLDS_SEEDED.inc(cur.rowcount)"]
  nonsol_sql --> rowcount
```

**Full ancestor path** is populated, not just the deepest FK. The CHECK constraint permits this at all non-solution scopes ("optional"), and the ToggleCache builds its key from the full path so spans match. One threshold row per distinct binding path the entity participates in.

**NOT EXISTS uses `IS NOT DISTINCT FROM`**, not `=`, because some binding fields (workflow_id, agent_id) can be NULL. Plain `=` returns NULL for NULL operands → NOT EXISTS returns true → re-insert on every run → unbounded growth. `IS NOT DISTINCT FROM` treats NULL as a value.

**NULL literals cast to `::float8`** in the SELECT — `INSERT...SELECT` doesn't always coerce bare `NULL` to the target column type (especially with `SELECT DISTINCT`).

## 4. Caching

```mermaid
flowchart TD
  start["pod start"] --> load["_load_slug_cache:<br/>SELECT slug, id FROM<br/>(solutions / endpoints / workflows /<br/>agents / components)"]
  load --> cache["self._slug_cache<br/>{entity_type: {slug: uuid}}"]
  cache --> batch["each batch"]
  batch --> hit{slug in cache?}
  hit -->|yes| reuse["0 PG round-trip"]
  hit -->|no| miss["UPSERT + RETURNING id"]
  miss --> stage["pending_cache_adds[type][slug] = uuid"]
  stage --> end_tx["after pg.commit():<br/>cache.update(pending_cache_adds)"]
```

| Cache | Bounded by | Eviction |
|---|---|---|
| `metric_catalog` (`self._catalog`) | catalog size (~22 rows) | None — pod lifetime |
| Slug → UUID (`self._slug_cache`) | total registry cardinality (~thousands) | None — pod lifetime; pod restart reloads |

No TTL. Registry entries don't disappear (admin soft-deletes via `is_active=false` leave the row in place; we don't read `is_active` for cache validity). Pod restart reloads from PG.

## 5. Steady-state SQL cost per batch

5,000 spans, all referencing entities already in the cache:

| Phase | SQL statements |
|---|---|
| 1 — registry | 0 (all cache hits) |
| 2 — bindings | ~5–10 UPSERTs (distinct paths) |
| 3 — observed_span_types | ~10–30 UPSERTs (distinct (entity, span_type) within batch) |
| 4 — thresholds | ~22 metrics × ~3 avg scopes = ~66 `NOT EXISTS`-guarded INSERTs, ~0 rows inserted |
| checkpoint | 1 UPSERT |

~70–100 round-trips per 5,000 spans. The Phase 4 NOT EXISTS scans are indexed lookups; sub-100 ms total.

First batch after pod start: same shape, but Phase 4 may insert many rows for any newly-bound entities. Backfill at startup catches the rest (registry entities present before the reconciler had a chance to seed them).

## 6. Topology + scaling

```mermaid
flowchart LR
  recon_pod["compass-worker-reconciler"] -->|"single replica<br/>strategy=Recreate"| pg[(PG)]
  ch[(CH compass_raw_spans)] -->|fetch_batch all span_types| recon_pod
```

| Knob | Value | Why |
|---|---|---|
| `replicas` | 1 | Two reconcilers race on UPSERTs |
| `strategy` | Recreate | Old pod terminated before new pod starts — guarantees single writer |
| `WORKER_PARTITION_COUNT` | unset | unpartitioned (`my_slots = [None]`) — reads all rows |
| Resources | 100m / 256Mi req, 500m / 512Mi lim | PG-bound, not CPU-bound |

The work is small and bursty — most batches in steady state are cheap. Spike on first deployment processing the 90d backlog; recovers automatically via the worker's "drain mode" (no sleep between full batches).

## 7. Observability

Adds to the standard worker metrics:

| Metric | Labels | Increments |
|---|---|---|
| `compass_reconciler_new_entities_total` | `entity_type` | After successful commit, per type of entity inserted via cache-miss |
| `compass_reconciler_thresholds_seeded_total` | `lens, scope` | Per `cur.rowcount` from each Phase 4 INSERT |

`compass_worker_checkpoint_lag_seconds{lens="reconciler"}` is the main health signal. Steady at 0–10s ≈ fine. Climbing without bound ≈ spans arriving faster than PG can keep up (unusual; PG is fast).

## 8. Failure modes

| Failure | Outcome |
|---|---|
| Partial commit failure | Whole batch rolled back, cache untouched, pod restart re-fetches from previous checkpoint |
| Two reconcilers running concurrently (e.g. manual `kubectl scale`) | Both try UPSERTs; UNIQUE constraints + ON CONFLICT serialize them — no data corruption, just wasted work. **Strategy=Recreate prevents this.** |
| Registry row deleted by admin while reconciler running | Cache stale → next batch's INSERT references missing FK → transaction fails → rollback. Pod restart reloads cache from PG. Mitigation: don't hard-delete; use `is_active=false` |
| `metric_catalog` table dropped | Catalog upsert at next pod start recreates it from SPECS. Until then, Phase 4 skips |
| `observed_span_types` corrupted | Phase 3 keeps writing fresh; coverage views fall back to "dark" if evidence missing. Run reconciler against the 90d backlog to rebuild |
| Skill files missing | Not applicable — reconciler has no model dependencies |

## 9. Adding a new metric / lens

```mermaid
flowchart LR
  step1["1. Declare MetricSpec<br/>in lens file<br/>(threshold=True)"] --> step2["2. If predicate is new:<br/>add to PREDICATE_INFO<br/>in catalog.py"]
  step2 --> step3["3. Redeploy reconciler"]
  step3 --> step4["At startup:<br/>metric_catalog upsert<br/>picks up new row"]
  step4 --> step5["At first batch:<br/>Phase 4 seeds thresholds<br/>for every applicable entity"]
```

Missing the predicate registration is a deploy-time error — `build_catalog_rows()` raises before the first batch.

## 10. State after reconciler runs

```
PG state                       Source
─────────────────────────────  ─────────────────────────────────────
solutions / endpoints / ...    Reconciler Phase 1 (auto-discovered)
bindings                        Reconciler Phase 2
observed_span_types             Reconciler Phase 3
thresholds (is_active=false)   Reconciler Phase 4 + backfill
thresholds (is_active=true)    User action via Coverage UI
metric_catalog                  Reconciler startup (from lens SPECS)
worker_checkpoints (reconciler) Reconciler save_checkpoint
```

Lens workers don't write to PG except `worker_checkpoints`. Reconciler doesn't write to CH at all.
