#!/usr/bin/env python3
"""Validate the Performance worker against the existing mock data.

It runs PerformanceWorker.compute over signal_raw_spans.csv and compares the
column-derivable metrics (latency, error_rate, timeout_count) against the values
already in signal_derived_metrics.csv for the same (span_id, scope). This proves
the real worker reproduces what's genuinely backed by the raw spans.
"""
import sys
import csv
from collections import defaultdict

from signal_worker.config import Config
from signal_worker.lenses.performance import PerformanceWorker

csv.field_size_limit(50_000_000)

RAW = sys.argv[1] if len(sys.argv) > 1 else "signal_raw_spans.csv"
DER = sys.argv[2] if len(sys.argv) > 2 else "signal_derived_metrics.csv"

# 1) run the worker
w = PerformanceWorker(Config())
produced = w.run_csv(RAW)

prod = defaultdict(dict)  # (span_id, scope) -> {metric: value}
metric_counts = defaultdict(int)
for r in produced:
    prod[(r["span_id"], r["scope"])][r["metric"]] = r["value"]
    metric_counts[r["metric"]] += 1

# 2) load mock derived for the same metrics
mock = defaultdict(dict)
with open(DER, newline="") as f:
    for r in csv.DictReader(f):
        if r["metric"] in ("latency", "error_rate", "timeout_count"):
            mock[(r["span_id"], r["scope"])][r["metric"]] = float(r["value"])

# 3) compare
def compare(metric, tol):
    n = ok = 0
    worst = 0.0
    for key, mvals in mock.items():
        if metric not in mvals:
            continue
        pv = prod.get(key, {}).get(metric)
        if pv is None:
            continue
        n += 1
        diff = abs(pv - mvals[metric])
        worst = max(worst, diff)
        if diff <= tol:
            ok += 1
    pct = 100.0 * ok / n if n else 0.0
    print(f"  {metric:14} compared={n:>7}  within_tol={pct:6.2f}%  max_diff={worst:.4f}")

print("== Performance worker output ==")
print(f"  total metric rows produced: {len(produced)}")
print("  rows per metric:")
for m in sorted(metric_counts):
    print(f"    {m:22} {metric_counts[m]:>7}")
print()
print("== match vs mock derived (column-derivable metrics) ==")
compare("latency", tol=1.5)        # ms rounding between stored ms timestamps and generated duration
compare("error_rate", tol=0.0)
compare("timeout_count", tol=0.0)

# 4) which mock perf metrics are NOT reproduced (need instrumented raw attributes)
mock_perf = set()
PERF = {"latency","error_rate","timeout_count","throughput","time_to_first_token","inter_token_latency",
        "queue_wait_time","scheduling_delay","retry_count","retry_delay","rate_limit_wait","rate_limit_hit",
        "records_processed","batch_size","concurrency","messages_in_flight"}
with open(DER, newline="") as f:
    for r in csv.DictReader(f):
        if r["metric"] in PERF:
            mock_perf.add(r["metric"])
produced_metrics = set(metric_counts)
print()
print("== perf metrics in mock but NOT yet produced by the worker ==")
print("   (these need the runtime to record the raw attribute in span metadata)")
for m in sorted(mock_perf - produced_metrics):
    print(f"    {m}")
