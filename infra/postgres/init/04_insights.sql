-- ============================================================================
-- Compass Observability — Insights Engine storage (PostgreSQL)
-- Applied on first container boot (docker-entrypoint-initdb.d), AFTER
-- 00_schema.sql (needs `thresholds`, `scope_level_enum`, `threshold_category_enum`).
-- Safe to apply manually to an existing DB — every object uses IF NOT EXISTS.
--
-- Stores the OUTPUT of the Insights Engine: prioritized insights derived from
-- compass_aggregated_metrics, grouped into incidents.
--
-- Design notes (see docs/insights.md):
--   * Insights are MUTABLE state (open → resolved) → Postgres, not ClickHouse.
--   * The entity is stored as the materialized path using the SAME STRING IDs
--     that appear in ClickHouse (solution_id='sol_support', etc.), NOT registry
--     UUIDs. The engine reads ClickHouse, so storing strings avoids a join on
--     write and makes "filter by entity" a direct column match.
--   * "lens" reuses threshold_category_enum (lens == threshold category).
--   * One OPEN row per signal is enforced by a partial unique index on
--     `fingerprint WHERE status='open'`. Reconciliation UPSERTs onto that key.
--   * Triage (claim/snooze/mute/false-positive) is DEFERRED — added later as a
--     purely-additive change when an API/UI exists.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Enums
-- ----------------------------------------------------------------------------
DO $$ BEGIN
  CREATE TYPE insight_detection_mode_enum AS ENUM ('threshold','baseline_drift');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE insight_severity_enum AS ENUM ('high','medium','low');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE insight_status_enum AS ENUM ('open','resolved');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- incidents — correlated insights grouped with a confidence score.
--   Created before `insights` because insights carry incident_id FK.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fingerprint  TEXT NOT NULL,                    -- stable identity for upsert (sol|env|root)
    solution_id  VARCHAR(64)  NOT NULL,
    environment  VARCHAR(32)  NOT NULL DEFAULT '',
    title        TEXT,                             -- LLM summary (optional)
    confidence   REAL         NOT NULL,            -- 0..1 grouping confidence
    severity     insight_severity_enum NOT NULL,   -- = max severity of members
    status       insight_status_enum   NOT NULL DEFAULT 'open',
    root_scope   scope_level_enum,                 -- suspected root entity level
    root_entity  VARCHAR(128),                     -- suspected root entity (string id)
    member_count INTEGER      NOT NULL DEFAULT 0,
    details      JSONB,
    opened_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT incident_confidence_range CHECK (confidence >= 0 AND confidence <= 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_incidents_open_fingerprint
    ON incidents (fingerprint) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_incidents_open
    ON incidents (solution_id, environment, status, opened_at);

COMMENT ON TABLE  incidents            IS 'Correlated insights grouped into one incident with a computed confidence score.';
COMMENT ON COLUMN incidents.fingerprint IS 'Stable identity for upsert: hash of solution|environment|root_scope|root_entity.';
COMMENT ON COLUMN incidents.confidence IS '0..1 grouping confidence (temporal overlap + path ancestry + co-movement).';

-- ----------------------------------------------------------------------------
-- insights — one prioritized signal at a given scope
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insights (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Deterministic identity for dedup/reconciliation:
    -- hash(lens|detection_mode|scope|full path|environment|metric|window).
    fingerprint     TEXT NOT NULL,

    lens            threshold_category_enum     NOT NULL,
    detection_mode  insight_detection_mode_enum NOT NULL,
    severity        insight_severity_enum       NOT NULL,
    status          insight_status_enum         NOT NULL DEFAULT 'open',

    -- Entity (materialized path; STRING ids matching compass_aggregated_metrics).
    -- "" for path levels shallower than `scope` (matches path_cols convention).
    scope           scope_level_enum NOT NULL,
    solution_id     VARCHAR(64)  NOT NULL,
    endpoint        VARCHAR(64)  NOT NULL DEFAULT '',
    workflow_id     VARCHAR(64)  NOT NULL DEFAULT '',
    agent_id        VARCHAR(64)  NOT NULL DEFAULT '',
    component_id    VARCHAR(128) NOT NULL DEFAULT '',
    component_type  VARCHAR(32)  NOT NULL DEFAULT '',
    environment     VARCHAR(32)  NOT NULL DEFAULT '',

    -- The signal.
    metric          VARCHAR(64)  NOT NULL,
    time_window     VARCHAR(16)  NOT NULL,
    operator        VARCHAR(8),                   -- gt|lt (threshold mode)
    observed_value  DOUBLE PRECISION,             -- merged aggregated value
    threshold_value DOUBLE PRECISION,             -- crossed bound (threshold mode)
    baseline_value  DOUBLE PRECISION,             -- 7d baseline (drift mode)
    deviation       DOUBLE PRECISION,             -- fractional deviation (drift mode)
    threshold_id    UUID REFERENCES thresholds(id),  -- the rule that fired; NULL if rule gone

    recommendation  TEXT,                         -- LLM suggestion (optional)
    details         JSONB,                        -- mode-specific extras + snapshot

    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,

    incident_id     UUID REFERENCES incidents(id) ON DELETE SET NULL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One live insight per signal — reconciliation UPSERTs on this.
CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_open_fingerprint
    ON insights (fingerprint) WHERE status = 'open';

-- Filter hot paths: by entity, by lens+severity, recent, by incident.
CREATE INDEX IF NOT EXISTS idx_insights_entity
    ON insights (solution_id, scope, environment, status);
CREATE INDEX IF NOT EXISTS idx_insights_lens_sev
    ON insights (lens, severity, status);
CREATE INDEX IF NOT EXISTS idx_insights_recent
    ON insights (status, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_insights_incident
    ON insights (incident_id);

COMMENT ON TABLE  insights             IS 'Prioritized signals derived from compass_aggregated_metrics across the lenses.';
COMMENT ON COLUMN insights.fingerprint IS 'Deterministic identity (lens|mode|scope|path|env|metric|window). Dedup/reconciliation key.';
COMMENT ON COLUMN insights.details     IS 'JSON: detection-mode specifics + the metric snapshot the insight was derived from.';
