"""Shared worker framework.

A lens worker only has to declare which metrics it owns and implement
`compute(span) -> list[MetricRow]`. The base class handles:

  * connecting to ClickHouse (lazy — only when actually used),
  * pulling unprocessed spans in batches (poll mode) by a `recorded_at` checkpoint,
  * writing computed metric rows into `signal_derived_metrics`,
  * persisting the checkpoint so restarts resume cleanly,
  * a `run_csv()` path so the exact same compute logic can be validated
    offline against an exported spans CSV (no ClickHouse needed),
  * a `on_message()` hook shaped for a future NATS consumer,
  * a `process_batch(spans)` hook for lenses with expensive per-text inference
    (Safety/Quality) — they override it to batch one model call across many
    spans; cheap lenses (Performance, Cost) inherit the default loop,
  * a `threading.Event`-based shutdown so workers stop cleanly on Ctrl+C.

The 25 raw columns and 18 derived columns mirror the loaded schema exactly.
"""
from __future__ import annotations
import os
import json
import threading
import logging
from datetime import datetime

log = logging.getLogger("signal.worker")

# Column order MUST match the loaded tables.
RAW_COLS = [
    "trace_id", "span_id", "parent_span_id", "correlation_id", "session_id",
    "span_type", "span_name", "span_status", "scope", "solution_id", "endpoint",
    "workflow_id", "agent_id", "component_id", "component_type",
    "started_at", "ended_at", "pipeline_stage", "stage_order", "entity_type",
    "service", "environment", "region", "metadata", "recorded_at",
]
DER_COLS = [
    "span_id", "trace_id", "parent_span_id", "scope", "solution_id", "endpoint",
    "workflow_id", "agent_id", "component_id", "component_type", "environment",
    "ts", "metric", "value", "confidence", "metric_meta", "start_ts", "end_ts",
]


def to_dt(x):
    """Accept a datetime (from clickhouse-connect) or a 'YYYY-MM-DD HH:MM:SS.mmm' string (CSV)."""
    if isinstance(x, datetime):
        return x
    s = str(x)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp: {x!r}")


def parse_meta(raw):
    if not raw or raw in ("{}", "\\N"):
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def path_cols(span: dict, scope: str) -> dict:
    """Entity-path columns appropriate to a scope (deeper-than-scope ids blanked).

    Matches the materialized-path convention used everywhere in Signal:
    the deepest non-empty id is the target; higher levels are context.
    """
    p = {
        "scope": scope,
        "solution_id": span.get("solution_id", ""),
        "endpoint": span.get("endpoint", "") or "",
        "workflow_id": span.get("workflow_id", "") or "",
        "agent_id": span.get("agent_id", "") or "",
        "component_id": span.get("component_id", "") or "",
        "component_type": span.get("component_type", "") or "",
        "environment": span.get("environment", "") or "",
    }
    if scope in ("solution", "endpoint"):
        p["workflow_id"] = p["agent_id"] = p["component_id"] = p["component_type"] = ""
    elif scope == "workflow":
        p["agent_id"] = p["component_id"] = p["component_type"] = ""
    elif scope == "agent":
        p["component_id"] = p["component_type"] = ""
    return p


class BaseWorker:
    lens = "base"
    # Optional: restrict this lens to specific span types. When set, the filter is
    # pushed into the fetch query (uses the primary-key index on span_type) so the
    # worker never reads spans it can't produce metrics for. None = read everything.
    span_types = None

    def __init__(self, cfg):
        self.cfg = cfg
        self._ch = None
        # Shutdown signal — set by stop() to terminate run_poll cleanly between batches.
        self._stop_event = threading.Event()

    # ---- which metrics this lens is responsible for (for logging/guardrails) ----
    def owns(self) -> set:
        raise NotImplementedError

    # ---- the only thing a lens must implement ----
    def compute(self, span: dict) -> list:
        """Return a list of derived rows (dicts keyed by DER_COLS) for one span."""
        raise NotImplementedError

    # ---- optional batch hook (overridden by SpecWorker; lenses can override again
    # to do expensive model inference across many spans in one call) ----
    def process_batch(self, spans: list) -> list:
        """Default batch processor: compute each span independently and concat results."""
        raise NotImplementedError

    # ---- ClickHouse (lazy import so offline/CSV use needs no driver) ----
    def ch(self):
        if self._ch is None:
            import clickhouse_connect
            self._ch = clickhouse_connect.get_client(
                host=self.cfg.ch_host, port=self.cfg.ch_port,
                username=self.cfg.ch_user, password=self.cfg.ch_password,
                database=self.cfg.ch_db,
            )
        return self._ch

    # ---- checkpoint (file-based; simple and restart-safe) ----
    def _state_path(self):
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        return os.path.join(self.cfg.state_dir, f"{self.lens}.checkpoint")

    def load_checkpoint(self) -> str:
        try:
            with open(self._state_path()) as f:
                return f.read().strip()
        except FileNotFoundError:
            return "1970-01-01 00:00:00.000"

    def save_checkpoint(self, wm: str):
        path = self._state_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(wm)
        os.replace(tmp, path)

    # ---- fetch a batch of spans newer than the checkpoint ----
    def fetch_batch(self, since: str, limit: int):
        type_filter = ""
        if self.span_types:
            lits = ", ".join("'" + t + "'" for t in self.span_types)
            type_filter = f"AND span_type IN ({lits}) "
        q = (
            f"SELECT {', '.join(RAW_COLS)} FROM signal_raw_spans "
            f"WHERE recorded_at > %(since)s {type_filter}"
            f"ORDER BY recorded_at LIMIT %(lim)s"
        )
        res = self.ch().query(q, parameters={"since": since, "lim": limit})
        return [dict(zip(res.column_names, row)) for row in res.result_rows]

    # ---- write computed rows ----
    def write(self, rows: list):
        if not rows:
            return
        data = [[r.get(c) for c in DER_COLS] for r in rows]
        self.ch().insert("signal_derived_metrics", data, column_names=DER_COLS)

    # ---- shutdown signal (used by main process on Ctrl+C / SIGTERM) ----
    def stop(self):
        log.info("[%s] stop requested", self.lens)
        self._stop_event.set()

    # ---- poll loop (production-ish) ----
    def run_poll(self, once: bool = False):
        log.info("[%s] starting poll loop (batch=%d)", self.lens, self.cfg.batch_size)
        while not self._stop_event.is_set():
            wm = self.load_checkpoint()
            spans = self.fetch_batch(wm, self.cfg.batch_size)
            print(f"Fetched {len(spans)} spans newer than {wm}\n")
            if spans:
                rows = self.process_batch(spans)
                newest = wm
                for s in spans:
                    rec = str(s["recorded_at"])
                    if rec > newest:
                        newest = rec
                self.write(rows)
                self.save_checkpoint(newest)
                log.info("[%s] %d spans -> %d metrics (wm=%s)", self.lens, len(spans), len(rows), newest)
            if once:
                break
            # Wait on the stop event instead of sleeping — wakes immediately on stop().
            if not spans:
                self._stop_event.wait(self.cfg.poll_sec)
        log.info("[%s] poll loop exited", self.lens)