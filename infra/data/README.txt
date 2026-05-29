Put the two telemetry CSVs here before running scripts\load_clickhouse.ps1:

    signal_raw_spans.csv
    signal_derived_metrics.csv

These are NOT bundled in the infra zip because of their size
(raw ~8 MB, derived ~140 MB). They are produced by the data generator
and are the same files you loaded previously.
