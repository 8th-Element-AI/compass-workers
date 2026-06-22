"""HTTP server + Prometheus metrics for worker pods.

Single port (default 8080), three endpoints:

  GET /healthz   liveness  — 200 if the process is running
  GET /readyz    readiness — 200 once run_poll has started, 503 before
  GET /metrics   prometheus exposition format

All counters/gauges/histograms are labeled by (lens, slot) so a single
Prometheus query can sum or group-by across pods and partitions. For
unpartitioned lenses (single-pod), slot="all".

The server runs as a daemon thread; SIGTERM kills the main thread and the
HTTP server dies with it. K8s waits up to terminationGracePeriodSeconds
for the in-flight batch to drain before SIGKILL.
"""
from __future__ import annotations

import http.server
import logging
import threading

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

log = logging.getLogger("compass.worker.observability")

# ───────────────────────────────────────────────────────────────────
# Metrics — all labeled by (lens, slot). slot="all" for unpartitioned.
# ───────────────────────────────────────────────────────────────────
BATCHES_TOTAL = Counter(
    "compass_worker_batches_total",
    "Total worker batches processed.",
    ["lens", "slot", "result"],  # result: success | error | empty
)

SPANS_PROCESSED = Counter(
    "compass_worker_spans_processed_total",
    "Spans fetched from ClickHouse.",
    ["lens", "slot"],
)

ROWS_EMITTED = Counter(
    "compass_worker_rows_emitted_total",
    "Derived rows successfully written.",
    ["lens", "slot"],
)

SKIPPED_AT_GATE = Counter(
    "compass_worker_skipped_at_gate_total",
    "Spans dropped at Stage 1 because no active toggle matches.",
    ["lens", "slot"],
)

BATCH_DURATION = Histogram(
    "compass_worker_batch_duration_seconds",
    "Wall-clock time per process_batch() call.",
    ["lens", "slot"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)

WRITE_DURATION = Histogram(
    "compass_worker_write_duration_seconds",
    "Wall-clock time per CH insert.",
    ["lens", "slot"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10),
)

CHECKPOINT_LAG = Gauge(
    "compass_worker_checkpoint_lag_seconds",
    "Now - last successfully-processed span's recorded_at, per (lens, slot).",
    ["lens", "slot"],
)


# ───────────────────────────────────────────────────────────────────
# Readiness flag — set from run_poll once the loop is healthy
# ───────────────────────────────────────────────────────────────────
_ready = False
_ready_lock = threading.Lock()


def set_ready(value: bool) -> None:
    """Compass readiness state. Called once by run_poll() after the loop starts."""
    global _ready
    with _ready_lock:
        _ready = value
    log.info("[obs] readiness set to %s", value)


def _is_ready() -> bool:
    with _ready_lock:
        return _ready


# ───────────────────────────────────────────────────────────────────
# HTTP handler
# ───────────────────────────────────────────────────────────────────
class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence default access log spam — every probe would log otherwise

    def do_GET(self):
        if self.path == "/healthz":
            self._text(200, "ok\n")
        elif self.path == "/readyz":
            ok = _is_ready()
            self._text(200 if ok else 503, "ready\n" if ok else "not ready\n")
        elif self.path == "/metrics":
            payload = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._text(404, "not found\n")

    def _text(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ───────────────────────────────────────────────────────────────────
# Lifecycle
# ───────────────────────────────────────────────────────────────────
def start_server(port: int = 8080) -> http.server.ThreadingHTTPServer:
    """Start the obs HTTP server as a daemon thread. Idempotent-ish — called
    once at worker startup from run_worker.py."""
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="obs-http",
        daemon=True,
    )
    thread.start()
    log.info("[obs] http server listening on :%d (/healthz /readyz /metrics)", port)
    return server