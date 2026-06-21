-- ============================================================================
-- Signal Observability — ClickHouse v2 DDL
-- Drops the existing Signal objects, then creates the v2 tables + base-grain MV.
--
-- v2 changes baked in:
--   * explicit `scope` column (solution/endpoint/workflow/agent/component)
--   * unified `component_id` + `component_type` (instead of 6 separate *_id cols)
--   * `environment` column on all tables (prod/staging/canary)
--   * aggregated table = AggregatingMergeTree with aggregate-function states
--   * single base-grain MV (1 MINUTE) — coarser windows via read-time merge
--   * io_* columns ZSTD-compressed
--
-- BASE GRAIN: 1 MINUTE (see SMV). Change to 30 SECOND only if you need
-- sub-minute live tailing. The mock CSV data is grain-independent.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0. DATABASE — create and switch to `signal` (else objects land in `default`)
-- ----------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS signal;
USE signal;

-- ----------------------------------------------------------------------------
-- 0b. RESET — drop existing Signal objects (now scoped to the `signal` db)
--    (inspect first with:  SHOW TABLES FROM signal;)
--    Drop the MV(s) before the tables they read/write.
-- ----------------------------------------------------------------------------
DROP VIEW  IF EXISTS mv_agg_base;
DROP VIEW  IF EXISTS mv_agg_30s;
DROP VIEW  IF EXISTS mv_agg_5m;
DROP VIEW  IF EXISTS mv_agg_1h;
DROP VIEW  IF EXISTS mv_agg_1d;
DROP TABLE IF EXISTS signal_aggregated_metrics;
DROP TABLE IF EXISTS signal_derived_metrics;
DROP TABLE IF EXISTS signal_raw_spans;
-- To wipe the whole database instead (DESTRUCTIVE — removes everything in it):
--   DROP DATABASE IF EXISTS signal;  CREATE DATABASE signal;

-- ----------------------------------------------------------------------------
-- 1. signal_raw_spans  (immutable span tree)
-- ----------------------------------------------------------------------------
CREATE TABLE signal_raw_spans
(
    trace_id        String,
    span_id         String,
    parent_span_id  String DEFAULT '',
    correlation_id  String DEFAULT '',
    session_id      String DEFAULT '',

    -- Deterministic 16-way partition for horizontal worker scaling.
    -- Computed at ingestion from trace_id; used by lens workers to fetch
    -- a fixed slice of spans without coordination. See
    -- signal-workers/signal_worker/partition.py.
    partition_id    UInt8 MATERIALIZED cityHash64(trace_id) % 16,

    span_type       LowCardinality(String),   -- solution/workflow/agent/model_call/tool_call/embedding/retrieval/memory_read/skill_exec/parsing/validation
    span_name       String,
    span_status     LowCardinality(String),   -- ok/error/timeout
    scope           LowCardinality(String),   -- solution/endpoint/workflow/agent/component

    solution_id     String,
    endpoint        String DEFAULT '',
    workflow_id     String DEFAULT '',
    agent_id        String DEFAULT '',
    component_id    String DEFAULT '',                 -- unified
    component_type  LowCardinality(String) DEFAULT '', -- unified: model/tool/skill/function/knowledgebase/memory

    started_at      DateTime64(3, 'UTC'),
    ended_at        DateTime64(3, 'UTC'),
    -- latency_ms removed: computed in derived_metrics from (ended_at - started_at)

    pipeline_stage  LowCardinality(String) DEFAULT '',
    stage_order     Nullable(UInt8),
    entity_type     LowCardinality(String) DEFAULT '',

    service         LowCardinality(String) DEFAULT '',
    environment     LowCardinality(String),
    region          LowCardinality(String) DEFAULT '',

    -- All descriptor columns (llm_model, llm_provider, temperature, tool_name, kb_name, ...)
    -- are derivable from component_id via Postgres and are NOT stored here.
    -- Per-span payload (io text, token usage, retrieval chunks, finish_reason,
    -- temperature/max_tokens, error_type/code/message/source/severity, ...) lives in one JSON blob:
    metadata        String DEFAULT '' CODEC(ZSTD(3)),

    recorded_at     DateTime64(3, 'UTC') DEFAULT now64(3),

    INDEX idx_partition_id partition_id TYPE set(16) GRANULARITY 8
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(started_at)
ORDER BY (solution_id, span_type, started_at, trace_id, span_id)
TTL toDateTime(started_at) + INTERVAL 90 DAY;

-- ----------------------------------------------------------------------------
-- 2. signal_derived_metrics  (EAV — one row per metric per span)
-- ----------------------------------------------------------------------------
CREATE TABLE signal_derived_metrics
(
    span_id        String,
    trace_id       String DEFAULT '',                 -- links the metric to its trace (denormalized for per-trace queries)
    parent_span_id String DEFAULT '',                 -- parent span in the trace tree
    scope          LowCardinality(String),
    solution_id    String,
    endpoint       String DEFAULT '',
    workflow_id    String DEFAULT '',
    agent_id       String DEFAULT '',
    component_id   String DEFAULT '',
    component_type LowCardinality(String) DEFAULT '',
    environment    LowCardinality(String),

    ts             DateTime64(3, 'UTC'),
    metric         LowCardinality(String),
    value          Float64,
    confidence     Nullable(Float32),
    metric_meta    Nullable(String),

    start_ts       DateTime64(3, 'UTC'),
    end_ts         DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (solution_id, scope, metric, ts, component_id)
TTL toDateTime(ts) + INTERVAL 90 DAY
SETTINGS
    -- Idempotent worker writes: ClickHouse remembers the last N insert tokens
    -- and drops re-inserts that carry a matching insert_deduplication_token,
    -- which also prevents the MV from firing twice. Workers pass a deterministic
    -- token per batch (see signal_worker/base.py:run_poll). Window of 1000 is
    -- vastly more than the worst-case "crash between write and save_checkpoint"
    -- gap; we'd only need it to cover one stale batch.
    non_replicated_deduplication_window = 1000;

-- ----------------------------------------------------------------------------
-- 3. signal_aggregated_metrics  (AggregatingMergeTree — aggregate states)
--    No `window` column: one base grain, coarser windows merged on read.
-- ----------------------------------------------------------------------------
CREATE TABLE signal_aggregated_metrics
(
    scope          LowCardinality(String),
    solution_id    String,
    endpoint       String DEFAULT '',
    workflow_id    String DEFAULT '',
    agent_id       String DEFAULT '',
    component_id   String DEFAULT '',
    component_type LowCardinality(String) DEFAULT '',
    environment    LowCardinality(String),
    metric         LowCardinality(String),
    ts             DateTime64(3, 'UTC'),                       -- base-grain bucket start

    count          SimpleAggregateFunction(sum, UInt64),
    sum_value      SimpleAggregateFunction(sum, Float64),
    min_value      SimpleAggregateFunction(min, Float64),
    max_value      SimpleAggregateFunction(max, Float64),
    avg_value      AggregateFunction(avg, Float64),
    quantiles      AggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), Float64),
    avg_confidence AggregateFunction(avg, Nullable(Float32))
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (solution_id, scope, metric, ts, workflow_id, agent_id, component_id, component_type, environment)
TTL toDateTime(ts) + INTERVAL 365 DAY;

-- ----------------------------------------------------------------------------
-- 4. Materialized view — derived -> aggregated, BASE GRAIN = 1 MINUTE
--    Change INTERVAL 1 MINUTE -> 30 SECOND (both places) for sub-minute tailing.
-- ----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW mv_agg_base TO signal_aggregated_metrics AS
SELECT
    scope, solution_id, endpoint, workflow_id, agent_id, component_id, component_type, environment, metric,
    ts,                                            -- bucketed in the subquery below; name matches target column
    count()                                        AS count,
    sum(value)                                     AS sum_value,
    min(value)                                     AS min_value,
    max(value)                                     AS max_value,
    avgState(value)                                AS avg_value,
    quantilesTDigestState(0.5, 0.95, 0.99)(value)  AS quantiles,
    avgState(confidence)                           AS avg_confidence
FROM
(
    SELECT
        scope, solution_id, endpoint, workflow_id, agent_id, component_id, component_type, environment, metric,
        value, confidence,
        toStartOfInterval(ts, INTERVAL 1 MINUTE) AS ts   -- replaces raw ts with the 1-min bucket
    FROM signal_derived_metrics
)
GROUP BY
    scope, solution_id, endpoint, workflow_id, agent_id, component_id, component_type, environment, metric, ts;

-- ----------------------------------------------------------------------------
-- 5. LOAD ORDER  (the MV only sees inserts that happen AFTER it exists)
--    a) load raw_spans CSV
--    b) load derived_metrics CSV  -> mv_agg_base fires -> aggregated fills
--    If derived was loaded BEFORE the MV existed, backfill aggregated once:
-- ----------------------------------------------------------------------------
-- INSERT INTO signal_aggregated_metrics
-- SELECT
--     scope, solution_id, endpoint, workflow_id, agent_id, component_id, component_type, environment, metric, ts,
--     count(), sum(value), min(value), max(value), avgState(value),
--     quantilesTDigestState(0.5,0.95,0.99)(value), avgState(confidence)
-- FROM
-- (
--     SELECT scope, solution_id, endpoint, workflow_id, agent_id, component_id, component_type, environment, metric,
--            value, confidence, toStartOfInterval(ts, INTERVAL 1 MINUTE) AS ts
--     FROM signal_derived_metrics
-- )
-- GROUP BY
--     scope, solution_id, endpoint, workflow_id, agent_id, component_id, component_type, environment, metric, ts;

-- ----------------------------------------------------------------------------
-- 6. READ EXAMPLES  (merge base buckets up to each metric's window)
-- ----------------------------------------------------------------------------
-- p95 latency for agt_classify in sol_support over its 5m threshold window:
-- SELECT quantilesTDigestMerge(0.95)(quantiles) AS p95,
--        avgMerge(avg_value) AS avg, sum(count) AS n
-- FROM signal_aggregated_metrics
-- WHERE scope='agent' AND solution_id='sol_support'
--   AND workflow_id='wf_docunderstand' AND agent_id='agt_classify'
--   AND metric='latency' AND environment='prod'
--   AND ts >= now() - INTERVAL 5 MINUTE;
--
-- Daily cost by model (1d window):
-- SELECT component_id, sum(sum_value) AS total_cost
-- FROM signal_aggregated_metrics
-- WHERE scope='component' AND component_type='model' AND metric='cost'
--   AND ts >= now() - INTERVAL 1 DAY
-- GROUP BY component_id ORDER BY total_cost DESC;

-- ----------------------------------------------------------------------------
-- 7. OPTIONAL — coarse cascade tier (add later only if wide/1d reads get heavy)
-- ----------------------------------------------------------------------------
-- CREATE TABLE signal_aggregated_metrics_1h AS signal_aggregated_metrics;  -- same shape
-- CREATE MATERIALIZED VIEW mv_agg_1h TO signal_aggregated_metrics_1h AS
-- SELECT
--     scope, solution_id, endpoint, workflow_id, agent_id, component_id, component_type, environment, metric, ts,
--     sum(count) AS count, sum(sum_value) AS sum_value, min(min_value) AS min_value, max(max_value) AS max_value,
--     avgMergeState(avg_value) AS avg_value,
--     quantilesTDigestMergeState(0.5,0.95,0.99)(quantiles) AS quantiles,
--     avgMergeState(avg_confidence) AS avg_confidence
-- FROM
-- (
--     SELECT * EXCEPT (ts), toStartOfInterval(ts, INTERVAL 1 HOUR) AS ts FROM signal_aggregated_metrics
-- )
-- GROUP BY scope, solution_id, endpoint, workflow_id, agent_id, component_id, component_type, environment, metric, ts;
