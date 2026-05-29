# Quick row-count check across both stores.
param(
  [string]$PgContainer = "signal-postgres",
  [string]$ChContainer = "signal-clickhouse"
)
Write-Host "== Postgres (config) ==" -ForegroundColor Cyan
docker exec $PgContainer psql -U postgres -d signal -c @"
SELECT 'solutions' t,count(*) FROM solutions
UNION ALL SELECT 'endpoints',count(*) FROM endpoints
UNION ALL SELECT 'workflows',count(*) FROM workflows
UNION ALL SELECT 'agents',count(*) FROM agents
UNION ALL SELECT 'components',count(*) FROM components
UNION ALL SELECT 'bindings',count(*) FROM bindings
UNION ALL SELECT 'thresholds',count(*) FROM thresholds ORDER BY 1;
"@

Write-Host "== ClickHouse (telemetry) ==" -ForegroundColor Cyan
docker exec $ChContainer clickhouse-client --database signal --query @"
SELECT 'raw' t, count() c FROM signal_raw_spans
UNION ALL SELECT 'derived', count() FROM signal_derived_metrics
UNION ALL SELECT 'aggregated', count() FROM signal_aggregated_metrics
"@
