-- ============================================================================
-- Compass Coverage — Phase 2 views
-- ============================================================================
-- Pure read views over Phase 1's tables (metric_catalog, observed_span_types,
-- thresholds) joined with the registry. No new tables, no triggers, no DDL on
-- existing objects.
--
-- View dependencies (created in order):
--   coverage_entity_registry          ← unions the 5 registry tables
--   coverage_matrix                   ← per (entity, metric, state) — the core
--   coverage_entity_lens_summary      ← per (entity, lens)
--   coverage_entity_summary           ← per entity
--   coverage_fleet_lens_summary       ← per lens, fleet-level
--   coverage_fleet_summary            ← single row, fleet-level
--
-- State derivation (per cell):
--   dark      — no observed_span_types for this entity intersect the metric's
--               required_span_types
--   active    — at least one thresholds row exists with is_active=true
--               (any-path-active rule)
--   disabled  — applicable, but no active threshold row
-- ============================================================================

-- ----------------------------------------------------------------------------
-- coverage_entity_registry — union of the 5 registry tables.
-- Gives every entity a (entity_type, entity_id, slug, name) row.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW coverage_entity_registry AS
  SELECT 'solution'::scope_level_enum  AS entity_type,
         id        AS entity_id,
         solution_id  AS slug,
         solution_name AS name
    FROM solutions
  UNION ALL
  SELECT 'endpoint'::scope_level_enum,
         id, endpoint_id, endpoint_name FROM endpoints
  UNION ALL
  SELECT 'workflow'::scope_level_enum,
         id, workflow_id, workflow_name FROM workflows
  UNION ALL
  SELECT 'agent'::scope_level_enum,
         id, agent_id, agent_name FROM agents
  UNION ALL
  SELECT 'component'::scope_level_enum,
         id, component_id, component_name FROM components;

COMMENT ON VIEW coverage_entity_registry IS
  'Union of solutions/endpoints/workflows/agents/components into a uniform shape.';

-- ----------------------------------------------------------------------------
-- coverage_matrix — per (entity, metric) cell with derived state.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW coverage_matrix AS
WITH applicable AS (
    -- One row per (entity, metric) where the metric's applicable_scopes
    -- contains the entity's scope.
    SELECT
        r.entity_type,
        r.entity_id,
        r.slug,
        r.name,
        mc.metric,
        mc.lens,
        mc.required_span_types
      FROM coverage_entity_registry r
      CROSS JOIN metric_catalog mc
     WHERE r.entity_type = ANY(mc.applicable_scopes)
),
with_evidence AS (
    -- For each (entity, metric), does the entity have observed spans that
    -- match the metric's required_span_types? '*' means any span counts.
    SELECT
        a.*,
        EXISTS (
            SELECT 1
              FROM observed_span_types ost
             WHERE ost.entity_type = a.entity_type
               AND ost.entity_id   = a.entity_id
               AND (
                     '*' = ANY(a.required_span_types)
                  OR ost.span_type = ANY(a.required_span_types)
               )
        ) AS has_evidence
      FROM applicable a
),
with_thresholds AS (
    -- Per (entity, metric): count threshold rows and active ones.
    SELECT
        we.*,
        COALESCE(tc.total_paths,  0) AS threshold_paths,
        COALESCE(tc.active_paths, 0) AS active_paths
      FROM with_evidence we
      LEFT JOIN LATERAL (
          SELECT
              COUNT(*) AS total_paths,
              COUNT(*) FILTER (WHERE t.is_active) AS active_paths
            FROM thresholds t
           WHERE t.metric_name = we.metric
             AND t.category    = we.lens
             AND t.scope       = we.entity_type
             AND (
                   (we.entity_type = 'solution'  AND t.solution_id  = we.entity_id) OR
                   (we.entity_type = 'endpoint'  AND t.endpoint_id  = we.entity_id) OR
                   (we.entity_type = 'workflow'  AND t.workflow_id  = we.entity_id) OR
                   (we.entity_type = 'agent'     AND t.agent_id     = we.entity_id) OR
                   (we.entity_type = 'component' AND t.component_id = we.entity_id)
             )
      ) tc ON TRUE
)
SELECT
    entity_type,
    entity_id,
    slug,
    name,
    metric,
    lens,
    threshold_paths,
    active_paths,
    has_evidence,
    CASE
        WHEN NOT has_evidence   THEN 'dark'
        WHEN active_paths > 0   THEN 'active'
        ELSE                         'disabled'
    END::text AS state
  FROM with_thresholds;

COMMENT ON VIEW coverage_matrix IS
  'One row per (entity, applicable metric) with derived state: dark | disabled | active.';

-- ----------------------------------------------------------------------------
-- coverage_entity_lens_summary — per (entity, lens) rollup.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW coverage_entity_lens_summary AS
SELECT
    entity_type,
    entity_id,
    slug,
    name,
    lens,
    COUNT(*)                                   AS total_metrics,
    COUNT(*) FILTER (WHERE state = 'active')   AS active_metrics,
    COUNT(*) FILTER (WHERE state = 'disabled') AS disabled_metrics,
    COUNT(*) FILTER (WHERE state = 'dark')     AS dark_metrics,
    -- score: active / applicable (excludes dark from the denominator).
    -- NULL when no applicable metrics — UI should render as "n/a" / hidden.
    CASE
      WHEN COUNT(*) FILTER (WHERE state IN ('active','disabled')) = 0 THEN NULL
      ELSE ROUND(
        100.0 * COUNT(*) FILTER (WHERE state = 'active')
              / COUNT(*) FILTER (WHERE state IN ('active','disabled')),
        1
      )
    END AS score_pct,
    -- lens_state: dark when all metrics are dark; active when ≥1 active;
    -- disabled when applicable but no active.
    CASE
      WHEN COUNT(*) FILTER (WHERE state IN ('active','disabled')) = 0 THEN 'dark'
      WHEN COUNT(*) FILTER (WHERE state = 'active') > 0              THEN 'active'
      ELSE                                                                 'disabled'
    END AS lens_state
  FROM coverage_matrix
 GROUP BY entity_type, entity_id, slug, name, lens;

COMMENT ON VIEW coverage_entity_lens_summary IS
  'Per (entity, lens) counts + score + lens_state in (dark, disabled, active).';

-- ----------------------------------------------------------------------------
-- coverage_entity_summary — per entity rollup.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW coverage_entity_summary AS
SELECT
    entity_type,
    entity_id,
    slug,
    name,
    -- Metric-level
    SUM(total_metrics)    AS total_metrics,
    SUM(active_metrics)   AS active_metrics,
    SUM(disabled_metrics) AS disabled_metrics,
    SUM(dark_metrics)     AS dark_metrics,
    -- Lens-level
    COUNT(*)                                            AS total_lenses,
    COUNT(*) FILTER (WHERE lens_state = 'active')       AS active_lenses,
    COUNT(*) FILTER (WHERE lens_state = 'disabled')     AS disabled_lenses,
    COUNT(*) FILTER (WHERE lens_state = 'dark')         AS dark_lenses,
    COUNT(*) FILTER (WHERE lens_state IN ('active','disabled')) AS applicable_lenses,
    -- Coverage scores
    CASE
      WHEN SUM(active_metrics + disabled_metrics) = 0 THEN NULL
      ELSE ROUND(
        100.0 * SUM(active_metrics)
              / SUM(active_metrics + disabled_metrics),
        1
      )
    END AS metric_coverage_pct,
    CASE
      WHEN COUNT(*) FILTER (WHERE lens_state IN ('active','disabled')) = 0 THEN NULL
      ELSE ROUND(
        100.0 * COUNT(*) FILTER (WHERE lens_state = 'active')
              / COUNT(*) FILTER (WHERE lens_state IN ('active','disabled')),
        1
      )
    END AS lens_coverage_pct
  FROM coverage_entity_lens_summary
 GROUP BY entity_type, entity_id, slug, name;

COMMENT ON VIEW coverage_entity_summary IS
  'Per entity: counts at metric and lens level, plus two coverage scores.';

-- ----------------------------------------------------------------------------
-- coverage_fleet_lens_summary — fleet-level rollup per lens.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW coverage_fleet_lens_summary AS
SELECT
    lens,
    COUNT(*)                                   AS total_cells,
    COUNT(*) FILTER (WHERE state = 'active')   AS active_cells,
    COUNT(*) FILTER (WHERE state = 'disabled') AS disabled_cells,
    COUNT(*) FILTER (WHERE state = 'dark')     AS dark_cells,
    COUNT(DISTINCT (entity_type, entity_id)) AS entities,
    CASE
      WHEN COUNT(*) FILTER (WHERE state IN ('active','disabled')) = 0 THEN NULL
      ELSE ROUND(
        100.0 * COUNT(*) FILTER (WHERE state = 'active')
              / COUNT(*) FILTER (WHERE state IN ('active','disabled')),
        1
      )
    END AS score_pct
  FROM coverage_matrix
 GROUP BY lens;

COMMENT ON VIEW coverage_fleet_lens_summary IS
  'One row per lens with fleet-level counts and score.';

-- ----------------------------------------------------------------------------
-- coverage_fleet_summary — single-row fleet rollup.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW coverage_fleet_summary AS
SELECT
    COUNT(DISTINCT (entity_type, entity_id)) AS total_entities,
    COUNT(*)                                 AS total_cells,
    COUNT(*) FILTER (WHERE state = 'active')   AS active_cells,
    COUNT(*) FILTER (WHERE state = 'disabled') AS disabled_cells,
    COUNT(*) FILTER (WHERE state = 'dark')     AS dark_cells,
    CASE
      WHEN COUNT(*) FILTER (WHERE state IN ('active','disabled')) = 0 THEN NULL
      ELSE ROUND(
        100.0 * COUNT(*) FILTER (WHERE state = 'active')
              / COUNT(*) FILTER (WHERE state IN ('active','disabled')),
        1
      )
    END AS overall_pct
  FROM coverage_matrix;

COMMENT ON VIEW coverage_fleet_summary IS
  'Single-row fleet rollup. Pair with coverage_fleet_lens_summary for per-lens scores.';
