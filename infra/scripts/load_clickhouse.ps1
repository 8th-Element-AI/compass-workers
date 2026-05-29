# Loads the telemetry CSVs into ClickHouse.
# The DDL (clickhouse/init/00_schema.sql) created the tables on first boot;
# this only loads data. The aggregated table fills itself via the materialized view.
#
# Put signal_raw_spans.csv and signal_derived_metrics.csv in infra\data\ first,
# then:  .\scripts\load_clickhouse.ps1
param(
  [string]$Container = "signal-clickhouse",
  [string]$DataDir   = "$PSScriptRoot\..\data"
)
$ErrorActionPreference = "Stop"

Write-Host "Copying CSVs into the container..."
docker cp "$DataDir\signal_raw_spans.csv"        "${Container}:/tmp/raw.csv"
docker cp "$DataDir\signal_derived_metrics.csv"  "${Container}:/tmp/derived.csv"

Write-Host "Loading signal_raw_spans..."
docker exec $Container sh -c "clickhouse-client --database signal --query 'INSERT INTO signal_raw_spans FORMAT CSVWithNames' < /tmp/raw.csv"

Write-Host "Loading signal_derived_metrics (this also fires the MV into the aggregated table)..."
docker exec $Container sh -c "clickhouse-client --database signal --query 'INSERT INTO signal_derived_metrics FORMAT CSVWithNames' < /tmp/derived.csv"

Write-Host "Done." -ForegroundColor Green
