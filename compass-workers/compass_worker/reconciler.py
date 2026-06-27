"""Reconciler worker — auto-discovery + auto-seed for coverage tracking.

Reads spans from compass_raw_spans, writes only to Postgres. Returns [] from
process_batch so the engine's CH write() is a no-op.

Per batch, in one transaction:
  1. UPSERT registry rows ONLY for cache misses (new entities). Cache hits
     skip the PG round-trip entirely.
  2. UPSERT bindings — one row per distinct materialized path.
  3. UPSERT observed_span_types per (entity, span_type), aggregated within
     the batch and propagated up the materialized path.
  4. Seed thresholds — ONLY when this batch produced cache misses (i.e.
     truly new entities). Steady-state batches skip Phase 4 entirely.

Caching (built lazily on first batch, then maintained incrementally):
  * Slug cache: per entity_type, slug -> UUID. Pre-loaded from PG on first
    batch via a single SELECT per registry table, then extended after every
    successful commit. Bounded by registry size (~thousands of entities).
  * Cache is updated AFTER pg.commit() succeeds — a rolled-back batch leaves
    the cache untouched, so the next batch retries cleanly.
  * Pod restart reloads the cache from PG. No TTL needed; registry entries
    don't disappear (admin soft-deletes leave the row in place).

Metrics (Prometheus):
  * compass_reconciler_new_entities_total{entity_type}
  * compass_reconciler_thresholds_seeded_total{lens,scope}

Checkpoint: worker_checkpoints row with lens='reconciler', partition_key='default'.
Replicas: 1.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

import psycopg
from prometheus_client import Counter

from .base import BaseWorker
from .catalog import upsert_metric_catalog, load_metric_catalog

log = logging.getLogger("compass.worker.reconciler")


# ---- Prometheus counters (module-level; registered once) ---------------------
NEW_ENTITIES = Counter(
    "compass_reconciler_new_entities_total",
    "Entities discovered for the first time and inserted into the registry.",
    ["entity_type"],
)
THRESHOLDS_SEEDED = Counter(
    "compass_reconciler_thresholds_seeded_total",
    "Threshold rows auto-seeded with is_active=false.",
    ["lens", "scope"],
)


# Entity types in dependency order — solutions first because endpoints FK them.
_ENTITY_TYPES = ("solution", "endpoint", "workflow", "agent", "component")


class ReconcilerWorker(BaseWorker):
    lens = "reconciler"
    span_types = None   # read every span_type

    def __init__(self, cfg):
        super().__init__(cfg)
        self._pg = None
        self._catalog: list[dict] | None = None
        # entity_type -> {slug: uuid}.  Populated on first batch.
        self._slug_cache: dict[str, dict[str, str]] = {
            t: {} for t in _ENTITY_TYPES
        }
        self._cache_loaded = False

    # ---- PG handle -----------------------------------------------------
    def _pg_conn(self):
        """Lazy PG connection; kept open across batches. autocommit OFF — every
        batch is a single transaction."""
        if self._pg is None or self._pg.closed:
            self._pg = psycopg.connect(self.cfg.pg_dsn, autocommit=False)
            log.info("[reconciler] PG connected")
        return self._pg

    def _ensure_catalog(self):
        """Upsert metric_catalog, cache it, prime slug cache, backfill thresholds
        for pre-existing entities. Idempotent — runs at most once per worker
        lifetime."""
        if self._catalog is not None:
            return
        pg = self._pg_conn()
        upsert_metric_catalog(pg)
        self._catalog = load_metric_catalog(pg)
        self._load_slug_cache(pg)
        log.info(
            "[reconciler] catalog loaded: %d metrics; cache pre-loaded: %s",
            len(self._catalog),
            {t: len(self._slug_cache[t]) for t in _ENTITY_TYPES},
        )
        self._backfill_thresholds(pg)

    def _backfill_thresholds(self, pg):
        """One-shot pass at worker startup: seed thresholds for every entity
        already in PG when the reconciler comes up.

        Why this is needed: the steady-state Phase 4 only seeds for entities
        that were cache misses *this batch* (i.e. truly new). That's the
        whole optimization. But entities can pre-exist in PG for reasons
        outside the reconciler's history — seeded data (01_registry_bindings.sql),
        an earlier deployment that ran without the reconciler, a restored
        backup, manual inserts. Without this backfill they'd be cache hits
        on every batch and never get their thresholds seeded.

        NOT EXISTS guards on the SELECT make this safe to re-run — a clean
        restart inserts 0 rows. Cost is proportional to (catalog size) ×
        (scopes per metric) × (cached entity count), bounded and one-time
        per pod lifetime.
        """
        total_by_type = {t: len(self._slug_cache[t]) for t in _ENTITY_TYPES}
        if not any(total_by_type.values()):
            log.info("[reconciler] backfill: registry is empty; skipping")
            return

        all_uuids = {
            etype: list(self._slug_cache[etype].values())
            for etype in _ENTITY_TYPES
        }
        seeded_total = 0
        try:
            with pg.cursor() as cur:
                for entry in self._catalog:
                    metric   = entry["metric"]
                    lens     = entry["lens"]
                    scopes   = entry["applicable_scopes"]
                    window   = entry["default_window"]
                    operator = entry["default_operator"]
                    for scope in scopes:
                        ent_ids = all_uuids.get(scope, [])
                        if not ent_ids:
                            continue
                        seeded = self._seed_one_scope(
                            cur, scope, ent_ids, lens, metric, window, operator,
                        )
                        if seeded:
                            THRESHOLDS_SEEDED.labels(lens=lens, scope=scope).inc(seeded)
                            seeded_total += seeded
            pg.commit()
        except Exception:
            pg.rollback()
            raise

        log.info(
            "[reconciler] backfill: seeded %d threshold rows across %d existing entities",
            seeded_total, sum(total_by_type.values()),
        )

    # ---- poll loop ------------------------------------------------------
    def run_poll(self, once: bool = False):
        """Override the BaseWorker poll loop to seed metric_catalog and prime
        the slug cache at STARTUP, before the first batch.

        Without this, both side effects would be deferred to the first non-empty
        batch — and on an empty ClickHouse the reconciler would poll forever
        with neither the catalog nor the cache populated.
        """
        self._ensure_catalog()
        super().run_poll(once=once)

    def _load_slug_cache(self, pg):
        """One SELECT per registry table on first batch. Bounded by registry
        size; sub-second even with thousands of entities."""
        with pg.cursor() as cur:
            for entity_type, table, slug_col in (
                ("solution",  "solutions",  "solution_id"),
                ("endpoint",  "endpoints",  "endpoint_id"),
                ("workflow",  "workflows",  "workflow_id"),
                ("agent",     "agents",     "agent_id"),
                ("component", "components", "component_id"),
            ):
                cur.execute(f"SELECT {slug_col}, id FROM {table}")
                self._slug_cache[entity_type] = {
                    row[0]: str(row[1]) for row in cur.fetchall()
                }
        self._cache_loaded = True
        # Also pre-commit cache to log how many entries we already had — useful
        # for diagnosing restart behavior.
        pg.commit()  # close the read-only transaction

    # ---- engine hook ---------------------------------------------------
    def process_batch(self, spans: list) -> list:
        """Run all phases in one PG transaction. Returns [] — nothing for CH."""
        if not spans:
            return []

        self._ensure_catalog()
        pg = self._pg_conn()

        # Staging area: cache additions only land in self._slug_cache AFTER
        # a successful commit. Rollback leaves the cache untouched.
        pending_cache_adds: dict[str, dict[str, str]] = {
            t: {} for t in _ENTITY_TYPES
        }

        try:
            with pg.cursor() as cur:
                slug_to_uuid = self._upsert_registry(
                    cur, spans, pending_cache_adds,
                )
                self._upsert_bindings(cur, spans, slug_to_uuid)
                self._record_observations(cur, spans, slug_to_uuid)
                # Phase 4 runs over every entity touched this batch (cache
                # hit or miss). NOT EXISTS guards make already-seeded entities
                # cost one indexed probe per metric/scope; new ones get their
                # rows in this same batch. Single-pass convergence.
                if slug_to_uuid:
                    self._seed_thresholds(cur, slug_to_uuid)
            pg.commit()
        except Exception:
            pg.rollback()
            raise

        # Commit succeeded — merge staged cache adds and bump new-entity metric.
        for etype, adds in pending_cache_adds.items():
            if adds:
                self._slug_cache[etype].update(adds)
                NEW_ENTITIES.labels(entity_type=etype).inc(len(adds))

        return []

    # ===================================================================
    # Phase 1 — registry UPSERTs (cache-miss only)
    # ===================================================================
    def _upsert_registry(
        self, cur, spans: list,
        pending: dict[str, dict[str, str]],
    ) -> dict[tuple[str, str], str]:
        """Return {(entity_type, slug): uuid} for every entity in this batch.

        Cache hits skip the PG round-trip. Cache misses trigger an UPSERT
        and the new UUID is staged in `pending` for post-commit merge into
        the live cache.
        """
        slug_to_uuid: dict[tuple[str, str], str] = {}

        # ---------- distinct slugs per level ----------
        sol_slugs = sorted({
            (s.get("solution_id") or "").strip()
            for s in spans if (s.get("solution_id") or "").strip()
        })
        endpoint_pairs = sorted({
            ((s.get("solution_id") or "").strip(), (s.get("endpoint") or "").strip())
            for s in spans
            if (s.get("solution_id") or "").strip() and (s.get("endpoint") or "").strip()
        })
        wf_slugs = sorted({
            (s.get("workflow_id") or "").strip()
            for s in spans if (s.get("workflow_id") or "").strip()
        })
        ag_slugs = sorted({
            (s.get("agent_id") or "").strip()
            for s in spans if (s.get("agent_id") or "").strip()
        })
        comp_seen: dict[str, str] = {}  # slug -> component_type
        for s in spans:
            cslug = (s.get("component_id") or "").strip()
            ctype = (s.get("component_type") or "").strip()
            if cslug and ctype:
                comp_seen.setdefault(cslug, ctype)

        cache = self._slug_cache

        # ---------- solutions ----------
        for slug in sol_slugs:
            uuid = cache["solution"].get(slug) or pending["solution"].get(slug)
            if uuid:
                slug_to_uuid[("solution", slug)] = uuid
                continue
            cur.execute("""
                INSERT INTO solutions (solution_id, solution_name)
                VALUES (%s, %s)
                ON CONFLICT (solution_id) DO UPDATE SET updated_at = NOW()
                RETURNING id;
            """, (slug, slug))
            uuid = str(cur.fetchone()[0])
            slug_to_uuid[("solution", slug)] = uuid
            pending["solution"][slug] = uuid

        # ---------- endpoints ----------
        for sol_slug, ep_slug in endpoint_pairs:
            uuid = cache["endpoint"].get(ep_slug) or pending["endpoint"].get(ep_slug)
            if uuid:
                slug_to_uuid[("endpoint", ep_slug)] = uuid
                continue
            sol_uuid = slug_to_uuid.get(("solution", sol_slug))
            if not sol_uuid:
                continue
            cur.execute("""
                INSERT INTO endpoints (endpoint_id, solution_id, endpoint_name, path)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (endpoint_id) DO UPDATE SET
                    path       = COALESCE(endpoints.path, EXCLUDED.path),
                    updated_at = NOW()
                RETURNING id;
            """, (ep_slug, sol_uuid, ep_slug, ep_slug))
            uuid = str(cur.fetchone()[0])
            slug_to_uuid[("endpoint", ep_slug)] = uuid
            pending["endpoint"][ep_slug] = uuid

        # ---------- workflows ----------
        for slug in wf_slugs:
            uuid = cache["workflow"].get(slug) or pending["workflow"].get(slug)
            if uuid:
                slug_to_uuid[("workflow", slug)] = uuid
                continue
            cur.execute("""
                INSERT INTO workflows (workflow_id, workflow_name)
                VALUES (%s, %s)
                ON CONFLICT (workflow_id) DO UPDATE SET updated_at = NOW()
                RETURNING id;
            """, (slug, slug))
            uuid = str(cur.fetchone()[0])
            slug_to_uuid[("workflow", slug)] = uuid
            pending["workflow"][slug] = uuid

        # ---------- agents ----------
        for slug in ag_slugs:
            uuid = cache["agent"].get(slug) or pending["agent"].get(slug)
            if uuid:
                slug_to_uuid[("agent", slug)] = uuid
                continue
            cur.execute("""
                INSERT INTO agents (agent_id, agent_name)
                VALUES (%s, %s)
                ON CONFLICT (agent_id) DO UPDATE SET updated_at = NOW()
                RETURNING id;
            """, (slug, slug))
            uuid = str(cur.fetchone()[0])
            slug_to_uuid[("agent", slug)] = uuid
            pending["agent"][slug] = uuid

        # ---------- components ----------
        for slug, ctype in sorted(comp_seen.items()):
            uuid = cache["component"].get(slug) or pending["component"].get(slug)
            if uuid:
                slug_to_uuid[("component", slug)] = uuid
                continue
            cur.execute("""
                INSERT INTO components (component_id, component_name, component_type)
                VALUES (%s, %s, %s::component_type_enum)
                ON CONFLICT (component_id) DO UPDATE SET updated_at = NOW()
                RETURNING id;
            """, (slug, slug, ctype))
            uuid = str(cur.fetchone()[0])
            slug_to_uuid[("component", slug)] = uuid
            pending["component"][slug] = uuid

        return slug_to_uuid

    # ===================================================================
    # Phase 2 — bindings
    # ===================================================================
    def _upsert_bindings(self, cur, spans: list, slug_to_uuid: dict):
        """One row per distinct materialized path. solution_id + endpoint_id
        are NOT NULL on bindings; paths missing either are skipped.

        Bindings are NOT cached — distinct-path cardinality per batch is small
        and the UNIQUE index on bindings makes the UPSERT a single index probe.
        """
        paths = set()
        for s in spans:
            sol = (s.get("solution_id")  or "").strip()
            ep  = (s.get("endpoint")     or "").strip()
            wf  = (s.get("workflow_id")  or "").strip()
            ag  = (s.get("agent_id")     or "").strip()
            co  = (s.get("component_id") or "").strip()
            if not sol or not ep:
                continue
            paths.add((sol, ep, wf, ag, co))

        for sol, ep, wf, ag, co in sorted(paths):
            sol_id = slug_to_uuid.get(("solution", sol))
            ep_id  = slug_to_uuid.get(("endpoint", ep))
            if not sol_id or not ep_id:
                continue
            wf_id = slug_to_uuid.get(("workflow", wf))  if wf else None
            ag_id = slug_to_uuid.get(("agent", ag))     if ag else None
            co_id = slug_to_uuid.get(("component", co)) if co else None
            cur.execute("""
                INSERT INTO bindings
                    (solution_id, endpoint_id, workflow_id, agent_id, component_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (solution_id, endpoint_id, workflow_id, agent_id, component_id)
                  DO UPDATE SET updated_at = NOW();
            """, (sol_id, ep_id, wf_id, ag_id, co_id))

    # ===================================================================
    # Phase 3 — observed_span_types (aggregated, propagated up the path)
    # ===================================================================
    def _record_observations(self, cur, spans: list, slug_to_uuid: dict):
        """One UPSERT per distinct (entity, span_type) — the per-span fan-out
        rides on the sample_count column, not on round-trip count."""
        agg: dict[tuple[str, str, str], int] = defaultdict(int)
        for s in spans:
            st = (s.get("span_type") or "").strip()
            if not st:
                continue
            for level, slug_key in (
                ("solution",  "solution_id"),
                ("endpoint",  "endpoint"),
                ("workflow",  "workflow_id"),
                ("agent",     "agent_id"),
                ("component", "component_id"),
            ):
                slug = (s.get(slug_key) or "").strip()
                if not slug:
                    continue
                ent_id = slug_to_uuid.get((level, slug))
                if not ent_id:
                    continue
                agg[(level, ent_id, st)] += 1

        for (level, ent_id, st), count in agg.items():
            cur.execute("""
                INSERT INTO observed_span_types
                    (entity_type, entity_id, span_type, first_seen, last_seen, sample_count)
                VALUES (%s::scope_level_enum, %s, %s, NOW(), NOW(), %s)
                ON CONFLICT (entity_type, entity_id, span_type) DO UPDATE SET
                    last_seen    = NOW(),
                    sample_count = observed_span_types.sample_count + EXCLUDED.sample_count;
            """, (level, ent_id, st, count))

    # ===================================================================
    # Phase 4 — auto-seed thresholds (new entities only)
    # ===================================================================
    def _seed_thresholds(self, cur, slug_to_uuid: dict[tuple[str, str], str]):
        """Seed thresholds for every entity touched this batch.

        Targets ALL entities in slug_to_uuid — cache hit or miss. NOT EXISTS
        guards in _seed_one_scope make repeated calls for already-seeded
        entities cheap (one indexed probe per metric/scope, zero inserts).

        This is what gives us single-pass convergence: a cache-hit entity
        whose bindings just landed this batch gets its thresholds in the
        same transaction, not on the next pod restart.
        """
        if not self._catalog:
            return

        by_scope_ids: dict[str, list[str]] = defaultdict(list)
        for (etype, _slug), euuid in slug_to_uuid.items():
            by_scope_ids[etype].append(euuid)
        if not by_scope_ids:
            return

        for entry in self._catalog:
            metric   = entry["metric"]
            lens     = entry["lens"]
            scopes   = entry["applicable_scopes"]
            window   = entry["default_window"]
            operator = entry["default_operator"]
            for scope in scopes:
                ent_ids = by_scope_ids.get(scope, [])
                if not ent_ids:
                    continue
                seeded = self._seed_one_scope(
                    cur, scope, ent_ids, lens, metric, window, operator,
                )
                if seeded:
                    THRESHOLDS_SEEDED.labels(lens=lens, scope=scope).inc(seeded)

    @staticmethod
    def _seed_one_scope(
        cur, scope: str, ent_ids: Iterable[str],
        lens: str, metric: str, window: str, operator: str,
    ) -> int:
        """INSERT...SELECT for (metric, scope) over the given entity UUIDs.

        Returns the number of rows actually inserted (cur.rowcount), so we
        can increment the THRESHOLDS_SEEDED counter accurately.
        """
        if scope == "solution":
            cur.execute("""
                INSERT INTO thresholds
                    (category, metric_name, scope, time_window, operator,
                     warning_value, critical_value, solution_id, is_active)
                SELECT %s::threshold_category_enum, %s, 'solution'::scope_level_enum,
                       %s, %s, NULL::float8, NULL::float8, s.id, false
                  FROM solutions s
                 WHERE s.id = ANY(%s::uuid[])
                   AND NOT EXISTS (
                         SELECT 1 FROM thresholds t
                          WHERE t.solution_id = s.id
                            AND t.scope = 'solution'::scope_level_enum
                            AND t.metric_name = %s
                            AND t.category = %s::threshold_category_enum
                   );
            """, (lens, metric, window, operator, list(ent_ids), metric, lens))
            return cur.rowcount or 0

        fk_col = {
            "endpoint":  "endpoint_id",
            "workflow":  "workflow_id",
            "agent":     "agent_id",
            "component": "component_id",
        }[scope]
        # Populate the FULL ancestor path from bindings, not just the deepest
        # required FK. The toggle_cache builds its set-membership key from the
        # threshold's full path (joined to slug-bearing tables), and spans at
        # non-solution scopes carry the full path via path_cols() — so a
        # threshold row with NULL ancestors would never match a span and the
        # gate would reject everything. Per scope_fk_validity, ancestors are
        # OPTIONAL at non-solution scopes (and forbidden at solution scope),
        # so this is always valid.
        per_scope_cols = {
            "endpoint":  ("endpoint_id",),
            "workflow":  ("endpoint_id", "workflow_id"),
            "agent":     ("endpoint_id", "workflow_id", "agent_id"),
            "component": ("endpoint_id", "workflow_id", "agent_id", "component_id"),
        }[scope]
        per_scope_select = tuple(f"b.{c}" for c in per_scope_cols)
        cols_clause   = ", ".join(("solution_id", *per_scope_cols))
        select_clause = ", ".join(("b.solution_id", *per_scope_select))

        # NOT EXISTS must compare every path field — otherwise a component that
        # appears in multiple bindings (same comp, different agent/workflow)
        # would have its first path seeded, then the second path skipped
        # because "a row already exists for this component". We want one
        # threshold row per distinct path the entity participates in.
        #
        # IS NOT DISTINCT FROM, not = : a binding may have NULL workflow_id
        # or agent_id (endpoint → agent path with no workflow, etc.). The
        # seeded threshold row carries those NULLs. On a re-run, plain `=`
        # compares NULL=NULL which evaluates to NULL, NOT EXISTS returns
        # true, and we'd re-insert. IS NOT DISTINCT FROM treats NULL as a
        # value so the match works correctly.
        path_match = "\n                    AND ".join(
            f"t.{c} IS NOT DISTINCT FROM b.{c}" for c in per_scope_cols
        )

        sql = f"""
            INSERT INTO thresholds
                (category, metric_name, scope, time_window, operator,
                 warning_value, critical_value, {cols_clause}, is_active)
            SELECT DISTINCT
                 %s::threshold_category_enum, %s, %s::scope_level_enum, %s, %s,
                 NULL::float8, NULL::float8, {select_clause}, false
              FROM bindings b
             WHERE b.{fk_col} = ANY(%s::uuid[])
               AND NOT EXISTS (
                     SELECT 1 FROM thresholds t
                      WHERE t.scope    = %s::scope_level_enum
                        AND t.metric_name = %s
                        AND t.category = %s::threshold_category_enum
                        AND t.solution_id = b.solution_id
                        AND {path_match}
               );
        """
        cur.execute(sql, (
            lens, metric, scope, window, operator,
            list(ent_ids), scope, metric, lens,
        ))
        return cur.rowcount or 0