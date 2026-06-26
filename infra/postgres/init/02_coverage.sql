-- ============================================================================
-- Compass Coverage Tracking — Phase 1 schema additions
-- ============================================================================
-- Two new tables. No views or functions yet — those land in Phase 2.
--
-- metric_catalog        Declares every threshold-able metric across all lenses.
--                       The single source of truth for what counts as
--                       "applicable" and which scopes a threshold can be set at.
--                       Upserted at reconciler startup from the lens SPECS.
--
-- observed_span_types   Evidence table: which span_types have been seen at
--                       which entity. Written by the reconciler; read by the
--                       Phase 2 coverage views to decide dark vs disabled.
--
-- Both tables are pure additions — no existing tables are modified.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- metric_catalog
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_catalog (
    metric              TEXT PRIMARY KEY,
    lens                threshold_category_enum NOT NULL,
    required_span_types TEXT[] NOT NULL,                -- {'model_call'} | {'*'} | ...
    applicable_scopes   scope_level_enum[] NOT NULL,    -- scopes a threshold can sit at
    inputs              TEXT[],                          -- docs only
    unit                VARCHAR(32),
    default_window      VARCHAR(16),
    default_operator    VARCHAR(8) NOT NULL DEFAULT 'gt',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  metric_catalog                    IS 'Threshold-able metrics across all lenses. Upserted at reconciler startup from SPECS.';
COMMENT ON COLUMN metric_catalog.required_span_types IS 'span_type values whose presence proves this metric is applicable to an entity. ''*'' = any span.';
COMMENT ON COLUMN metric_catalog.applicable_scopes  IS 'scope_level_enum values at which a threshold for this metric may be set.';
COMMENT ON COLUMN metric_catalog.default_window     IS 'Hint copied into the seeded thresholds row (e.g. ''5m'', ''1h'', ''1d'').';

-- ----------------------------------------------------------------------------
-- observed_span_types — applicability evidence
-- ----------------------------------------------------------------------------
-- One row per (entity_type, entity_id, span_type) actually observed in CH.
-- Materialized at every level by the reconciler: a model_call under a
-- component is also recorded against the agent / workflow / endpoint / solution
-- above it. This makes the Phase 2 applicability check one join instead of a
-- recursive walk.
--
-- Cardinality is bounded by (# entities) * (# distinct span_types ~10). Small.
CREATE TABLE IF NOT EXISTS observed_span_types (
    entity_type     scope_level_enum NOT NULL,
    entity_id       UUID             NOT NULL,
    span_type       TEXT             NOT NULL,
    first_seen      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    sample_count    BIGINT           NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_type, entity_id, span_type)
);

CREATE INDEX IF NOT EXISTS idx_observed_span_types_lastseen
    ON observed_span_types (last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_observed_span_types_by_span_type
    ON observed_span_types (span_type, entity_type);

COMMENT ON TABLE  observed_span_types               IS 'Applicability evidence: which span_types each entity has actually emitted. Written by reconciler; read by coverage views.';
COMMENT ON COLUMN observed_span_types.entity_type   IS 'scope_level_enum: solution | endpoint | workflow | agent | component';
COMMENT ON COLUMN observed_span_types.sample_count  IS 'Total spans observed at this (entity, span_type) since first_seen. Diagnostic only.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_thresholds_logical_key
  ON thresholds (
    category,
    metric_name,
    scope,
    solution_id,
    COALESCE(endpoint_id,  '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(workflow_id,  '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(agent_id,     '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(component_id, '00000000-0000-0000-0000-000000000000'::uuid)
  );