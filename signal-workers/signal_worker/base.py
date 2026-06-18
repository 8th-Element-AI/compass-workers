"""Shared worker framework.

A lens worker only has to declare which metrics it owns and implement
`compute(span) -> list[MetricRow]`. The base class handles:

  * connecting to ClickHouse (lazy — only when actually used),
  * pulling unprocessed spans in batches (poll mode) by a `recorded_at` checkpoint,
  * writing computed metric rows into `signal_derived_metrics`,
  * persisting the checkpoint to Postgres (worker_checkpoints table) so
    restarts and pod replacements resume cleanly,
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
    """Parse a metadata blob (str | dict | empty marker) into a dict; never returns None."""
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
    """Engine-agnostic worker shell. Subclasses (typically via SpecWorker) declare
    their metrics and implement compute(span) -> list[row]; this class handles
    the loop, the DB connections, the checkpoint, and graceful shutdown.

    Class attrs:
        lens: Short lens name ('performance', 'cost', 'safety', ...).
        span_types: Optional list of span_type values; when set, the filter is
            pushed into fetch_batch's SQL (None = read everything).
    """
    lens = "base"
    # Optional: restrict this lens to specific span types. When set, the filter is
    # pushed into the fetch query (uses the primary-key index on span_type) so the
    # worker never reads spans it can't produce metrics for. None = read everything.
    span_types = None

    def __init__(self, cfg):
        """Hold config and prepare lazy CH handle + shutdown event."""
        from .partition import compute_slots, is_partitioned

        self.cfg = cfg
        self._ch = None
        # Shutdown signal — set by stop() to terminate run_poll cleanly between batches.
        self._stop_event = threading.Event()

        # Slot ownership — same code path whether partitioned or not. When
        # is_partitioned is False, my_slots = [None] and the per-slot loop
        # collapses to one iteration with no partition filter.
        self.partitioned = is_partitioned(cfg.worker_partition_count)
        if self.partitioned:
            self.my_slots = compute_slots(
                cfg.worker_partition_index,
                cfg.worker_partition_count,
                cfg.worker_partition_total_slots,
            )
            log.info(
                "[%s] partition: pod=%d/%d owns_slots=%s",
                self.lens,
                cfg.worker_partition_index,
                cfg.worker_partition_count,
                self.my_slots,
            )
        else:
            self.my_slots = [None]  # sentinel: one iteration, no filter
            log.info("[%s] partition: single-pod (unpartitioned)", self.lens)

        self._current_slot = "all"  # for logging/metrics; updated each batch in run_poll
    # ---- which metrics this lens is responsible for (for logging/guardrails) ----
    def owns(self) -> set:
        """Set of metric names this lens emits. Subclasses must override."""
        raise NotImplementedError

    # ---- the only thing a lens must implement ----
    def compute(self, span: dict) -> list:
        """Return a list of derived rows (dicts keyed by DER_COLS) for one span."""
        raise NotImplementedError

    # ---- optional batch hook (overridden by SpecWorker; lenses can override again
    # to do expensive model inference across many spans in one call) ----
    def process_batch(self, spans: list) -> list:
        """Process a list of spans into derived rows.

        Default base impl defers to subclasses. SpecWorker provides the standard
        gate-filtered loop; lenses with batched ML inference (e.g. Safety)
        override again to share a single model call across many spans.

        Args:
            spans: Raw span dicts.
        """
        raise NotImplementedError

    # ---- ClickHouse (lazy import so offline/CSV use needs no driver) ----
    def ch(self):
        """Lazy ClickHouse client. Opened on first call; offline/CSV paths never call it."""
        if self._ch is None:
            import clickhouse_connect
            self._ch = clickhouse_connect.get_client(
                host=self.cfg.ch_host, port=self.cfg.ch_port,
                username=self.cfg.ch_user, password=self.cfg.ch_password,
                database=self.cfg.ch_db,
            )
        return self._ch

    # ---- checkpoint (file-based; simple and restart-safe) ----
    def _checkpoint_store(self):
        """Lazy Postgres checkpoint store. Cached on self for the worker's lifetime."""
        if not hasattr(self, "_ckpt_store"):
            from .checkpoint import PostgresCheckpointStore
            self._ckpt_store = PostgresCheckpointStore(self.cfg.pg_dsn)
        return self._ckpt_store

    def load_checkpoint(self):
        """Return this lens's saved high-checkpoint from PG, or the epoch default if none.

        Returns:
            A 'YYYY-MM-DD HH:MM:SS.mmm' string usable as fetch_batch(since=...).
        """
        return self._checkpoint_store().load(self.lens)

    def save_checkpoint(self, cp):
        """Atomically UPSERT this lens's high-checkpoint into PG.

        Args:
            wm: New high-checkpoint string ('YYYY-MM-DD HH:MM:SS.mmm').
        """
        self._checkpoint_store().save(self.lens, cp)
    
    def _partition_key_for(self, slot: int | None) -> str:
        """Pick the worker_checkpoints.partition_key for this slot."""
        from .partition import slot_partition_key
        if slot is None:
            return "default"
        return slot_partition_key(slot)

    # ---- fetch a batch of spans newer than the checkpoint ----
    def fetch_batch(self, since: str, limit: int, slot: int | None = None,):
        """Fetch spans with recorded_at > since, optionally filtered to a slot.

        Args:
            since: Checkpoint string from load_checkpoint().
            limit: Maximum spans to return.
            slot: If set, restrict to partition_id = slot. None = all rows
                (used by unpartitioned lenses).
        """
        partition_clause = (
            "" if slot is None else f"AND partition_id = {slot}"
        )
        span_types_clause = ""
        params = {"since": since, "lim": limit}

        if self.span_types is not None:
            in_list = ",".join(f"'{t}'" for t in self.span_types)
            span_types_clause = f"AND span_type IN ({in_list})"

        q = f"""
            SELECT *
            FROM signal_raw_spans
            WHERE recorded_at > %(since)s
              {partition_clause}
              {span_types_clause}
            ORDER BY recorded_at
            LIMIT %(lim)s
        """
        res = self.ch().query(q, parameters=params)
        return [dict(zip(res.column_names, row)) for row in res.result_rows]

    # ---- write computed rows ----
    def write(self, rows: list, dedup_token: str | None = None):
        """Insert derived-metric rows into ClickHouse.

        Args:
            rows: Derived-row dicts keyed by DER_COLS. Empty list = no-op.
            dedup_token: Deterministic per-batch token. When supplied, CH drops
                duplicate inserts silently and the MV does not fire on them —
                this is what makes restart safe (see run_poll).
        """
        if not rows:
            return
        data = [[r.get(c) for c in DER_COLS] for r in rows]
        settings = {}
        if dedup_token:
            settings["insert_deduplication_token"] = dedup_token
        self.ch().insert(
            "signal_derived_metrics",
            data,
            column_names=DER_COLS,
            settings=settings,
        )
        
    # ---- shutdown signal (used by main process on Ctrl+C / SIGTERM) ----
    def stop(self):
        """Request graceful shutdown. run_poll exits at the next batch boundary."""
        log.info("[%s] stop requested", self.lens)
        self._stop_event.set()

    # ---- poll loop (production-ish) ----
    def run_poll(self, once: bool = False):
        """Production poll loop: per-slot fetch → process → write → checkpoint.

        For unpartitioned lenses (single pod, no partition filter), my_slots
        is [None] and this collapses to the previous single-iteration loop.

        For partitioned lenses, each iteration walks every slot the pod owns,
        processing what's available in each. Slot checkpoints are independent,
        so a slow slot doesn't block fast ones, and pod-count changes are
        absorbed automatically — a new pod inheriting slot 7 reads slot 7's
        checkpoint and resumes from there.

        Args:
            once: If True, process at most one non-empty batch and return.
        """
        from datetime import datetime, timezone
        from .observability import (
            BATCHES_TOTAL,
            SPANS_PROCESSED,
            ROWS_EMITTED,
            BATCH_DURATION,
            WRITE_DURATION,
            CHECKPOINT_LAG,
            set_ready,
        )

        log.info(
            "[%s] starting poll loop (batch=%d slots=%s)",
            self.lens, self.cfg.batch_size,
            "all" if not self.partitioned else self.my_slots,
        )
        print(f"[{self.lens}] starting poll loop (batch={self.cfg.batch_size} "
              f"slots={'all' if not self.partitioned else self.my_slots})")
        set_ready(True)

        while not self._stop_event.is_set():
            any_processed = False

            for slot in self.my_slots:
                if self._stop_event.is_set():
                    break

                slot_label = "all" if slot is None else str(slot)
                pk = self._partition_key_for(slot)

                # Per-slot checkpoint + lag gauge
                wm = self._checkpoint_store().load(self.lens, partition_key=pk)
                try:
                    wm_dt = datetime.strptime(wm[:23], "%Y-%m-%d %H:%M:%S.%f")\
                        .replace(tzinfo=timezone.utc)
                    CHECKPOINT_LAG.labels(lens=self.lens, slot=slot_label).set(
                        (datetime.now(timezone.utc) - wm_dt).total_seconds()
                    )
                except Exception:
                    pass

                spans = self.fetch_batch(wm, self.cfg.batch_size, slot=slot)
                print(f"[{self.lens}:s{slot_label}] fetched {len(spans)} "
                      f"spans newer than {wm}")

                if not spans:
                    BATCHES_TOTAL.labels(
                        lens=self.lens, slot=slot_label, result="empty"
                    ).inc()
                    continue

                any_processed = True
                SPANS_PROCESSED.labels(lens=self.lens, slot=slot_label).inc(len(spans))

                try:
                    self._current_slot = slot_label
                    with BATCH_DURATION.labels(lens=self.lens, slot=slot_label).time():
                        rows = self.process_batch(spans)

                    newest = wm
                    for s in spans:
                        rec = str(s["recorded_at"])
                        if rec > newest:
                            newest = rec

                    # Slot in the token so two pods owning different slots
                    # produce distinct tokens — no collisions in the dedup window.
                    dedup_token = f"{self.lens}:s{slot_label}:{newest}:{len(spans)}"

                    with WRITE_DURATION.labels(lens=self.lens, slot=slot_label).time():
                        self.write(rows, dedup_token=dedup_token)

                    self._checkpoint_store().save(self.lens, newest, partition_key=pk)

                    ROWS_EMITTED.labels(lens=self.lens, slot=slot_label).inc(len(rows))
                    BATCHES_TOTAL.labels(
                        lens=self.lens, slot=slot_label, result="success"
                    ).inc()

                    log.info(
                        "[%s:s%s] %d spans -> %d metrics (wm=%s token=%s)",
                        self.lens, slot_label, len(spans), len(rows), newest, dedup_token,
                    )
                except Exception:
                    BATCHES_TOTAL.labels(
                        lens=self.lens, slot=slot_label, result="error"
                    ).inc()
                    log.exception("[%s:s%s] batch failed; pod will restart",
                                  self.lens, slot_label)
                    raise

            if once and any_processed:
                break

            if not any_processed:
                self._stop_event.wait(self.cfg.poll_sec)

        log.info("[%s] poll loop exited", self.lens)