-- ============================================================================
-- Compass Observability — PostgreSQL schema (v5)
-- 5 registry tables (solutions, endpoints, workflows, agents, components)
-- + 2 use tables (bindings, thresholds).
-- Applied automatically on first container boot (docker-entrypoint-initdb.d),
-- BEFORE 01_registry_bindings.sql and 02_thresholds.sql.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid() (built-in on PG13+, harmless here)

-- ----------------------------------------------------------------------------
-- Enums
-- ----------------------------------------------------------------------------
DO $$ BEGIN
  CREATE TYPE component_type_enum AS ENUM
    ('model','tool','skill','function','knowledgebase','memory');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE threshold_category_enum AS ENUM
    ('performance','quality','cost','safety','outcomes');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE scope_level_enum AS ENUM
    ('solution','endpoint','workflow','agent','component');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- Registry: solutions
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS solutions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    solution_id   VARCHAR(64)  UNIQUE NOT NULL,
    solution_name VARCHAR(255) NOT NULL,
    description   TEXT,
    is_active     BOOLEAN     NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- Registry: endpoints
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endpoints (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint_id     VARCHAR(64)  UNIQUE NOT NULL,
    solution_id     UUID NOT NULL REFERENCES solutions(id),
    endpoint_name   VARCHAR(255) NOT NULL,
    path            VARCHAR(255),
    method          VARCHAR(16),
    auth_type       VARCHAR(32),
    request_schema  JSONB,
    response_schema JSONB,
    description     TEXT,
    is_active       BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_endpoints_solution ON endpoints (solution_id);

-- ----------------------------------------------------------------------------
-- Registry: workflows
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflows (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id      VARCHAR(64)  UNIQUE NOT NULL,
    workflow_name    VARCHAR(255) NOT NULL,
    workflow_version VARCHAR(32),
    description      TEXT,
    is_active        BOOLEAN     NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- Registry: agents
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id    VARCHAR(64)  UNIQUE NOT NULL,
    agent_name  VARCHAR(255) NOT NULL,
    description TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- Registry: components (unified: model | tool | skill | function | kb | memory)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS components (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    component_id   VARCHAR(128) UNIQUE NOT NULL,
    component_type component_type_enum NOT NULL,
    component_name VARCHAR(255) NOT NULL,
    provider       VARCHAR(64),
    pricing        JSONB,
    metadata       JSONB,
    description    TEXT,
    is_active      BOOLEAN     NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_components_type   ON components (component_type, is_active);
CREATE INDEX IF NOT EXISTS idx_components_prov   ON components (provider);
CREATE INDEX IF NOT EXISTS idx_components_meta   ON components USING GIN (metadata);

-- ----------------------------------------------------------------------------
-- Use: bindings (materialized path; deepest non-NULL FK is the target)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bindings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    solution_id  UUID NOT NULL REFERENCES solutions(id),
    endpoint_id  UUID NOT NULL REFERENCES endpoints(id),
    workflow_id  UUID REFERENCES workflows(id),
    agent_id     UUID REFERENCES agents(id),
    component_id UUID REFERENCES components(id),
    config       JSONB,
    is_active    BOOLEAN     NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- one row per distinct path (PG15+ NULLS NOT DISTINCT)
CREATE UNIQUE INDEX IF NOT EXISTS uq_bindings_path
    ON bindings (solution_id, endpoint_id, workflow_id, agent_id, component_id) NULLS NOT DISTINCT;
CREATE INDEX IF NOT EXISTS idx_bindings_endpoint  ON bindings (endpoint_id, is_active);
CREATE INDEX IF NOT EXISTS idx_bindings_agent     ON bindings (agent_id, is_active);
CREATE INDEX IF NOT EXISTS idx_bindings_component ON bindings (component_id, is_active);
CREATE INDEX IF NOT EXISTS idx_bindings_workflow  ON bindings (workflow_id, is_active);

-- ----------------------------------------------------------------------------
-- Use: thresholds (alerting limits per metric per scope)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS thresholds (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category       threshold_category_enum NOT NULL,
    metric_name    VARCHAR(64) NOT NULL,
    scope          scope_level_enum NOT NULL,
    time_window    VARCHAR(16) NOT NULL,
    operator       VARCHAR(8)  NOT NULL,
    warning_value  FLOAT,
    critical_value FLOAT,
    solution_id    UUID NOT NULL REFERENCES solutions(id),
    endpoint_id    UUID REFERENCES endpoints(id),
    workflow_id    UUID REFERENCES workflows(id),
    agent_id       UUID REFERENCES agents(id),
    component_id   UUID REFERENCES components(id),
    is_active      BOOLEAN     NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT scope_fk_validity CHECK (
        (scope = 'solution'  AND endpoint_id IS NULL AND workflow_id IS NULL AND agent_id IS NULL AND component_id IS NULL)
     OR (scope = 'endpoint'  AND endpoint_id IS NOT NULL AND workflow_id IS NULL AND agent_id IS NULL AND component_id IS NULL)
     OR (scope = 'workflow'  AND workflow_id IS NOT NULL AND agent_id IS NULL AND component_id IS NULL)
     OR (scope = 'agent'     AND agent_id IS NOT NULL AND component_id IS NULL)
     OR (scope = 'component' AND component_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_thresholds_main
    ON thresholds (solution_id, scope, metric_name, is_active);
-- per-category hot-path partial indexes
CREATE INDEX IF NOT EXISTS idx_thresholds_perf     ON thresholds (solution_id, scope, component_id, metric_name) WHERE category = 'performance' AND is_active = true;
CREATE INDEX IF NOT EXISTS idx_thresholds_quality  ON thresholds (solution_id, scope, component_id, metric_name) WHERE category = 'quality'     AND is_active = true;
CREATE INDEX IF NOT EXISTS idx_thresholds_cost     ON thresholds (solution_id, scope, component_id, metric_name) WHERE category = 'cost'        AND is_active = true;
CREATE INDEX IF NOT EXISTS idx_thresholds_safety   ON thresholds (solution_id, scope, component_id, metric_name) WHERE category = 'safety'      AND is_active = true;
CREATE INDEX IF NOT EXISTS idx_thresholds_outcomes ON thresholds (solution_id, scope, component_id, metric_name) WHERE category = 'outcomes'    AND is_active = true;
