# Quick row-count check across both stores.
param(
  [string]$PgContainer = "compass-postgres",
  [string]$ChContainer = "compass-clickhouse"
)
Write-Host "== Postgres (config) ==" -ForegroundColor Cyan
docker exec $PgContainer psql -U postgres -d compass -c @"
SELECT 'solutions' t,count(*) FROM solutions
UNION ALL SELECT 'endpoints',count(*) FROM endpoints
UNION ALL SELECT 'workflows',count(*) FROM workflows
UNION ALL SELECT 'agents',count(*) FROM agents
UNION ALL SELECT 'components',count(*) FROM components
UNION ALL SELECT 'bindings',count(*) FROM bindings
UNION ALL SELECT 'thresholds',count(*) FROM thresholds ORDER BY 1;
"@

Write-Host "== ClickHouse (telemetry) ==" -ForegroundColor Cyan
docker exec $ChContainer clickhouse-client --database compass --query @"
SELECT 'raw' t, count() c FROM compass_raw_spans
UNION ALL SELECT 'derived', count() FROM compass_derived_metrics
UNION ALL SELECT 'aggregated', count() FROM compass_aggregated_metrics
"@
