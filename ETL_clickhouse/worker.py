"""ETL worker — bridges otel_traces → compass_raw_spans.

Poll loop mirrors the compass-workers BaseWorker pattern exactly:
  * Load checkpoint from Postgres on every iteration.
  * Fetch up to BATCH_SIZE spans newer than the checkpoint.
  * Map each OTel span to a compass_raw_spans row.
  * Bulk-insert into compass_raw_spans.
  * Save the newest Timestamp as the new checkpoint.
  * If a full batch was returned (backlog), loop immediately — no sleep.
  * If a partial batch or nothing, sleep POLL_SEC before the next round.
  * Ctrl+C / SIGTERM calls stop(), which exits cleanly after the current batch.
  * --once flag processes one non-empty batch and returns (for testing).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .checkpoint import CheckpointStore
from .config import Config
from .mapper import RAW_COLS, map_span

log = logging.getLogger("etl.worker")


class ETLWorker:
    """Reads from otel_traces, formats spans, writes to compass_raw_spans."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ch = None
        self._stop_event = threading.Event()

    # ── ClickHouse client (lazy) ──────────────────────────────────────────────

    def _ch_client(self):
        if self._ch is None:
            import clickhouse_connect
            self._ch = clickhouse_connect.get_client(
                host=self.cfg.ch_host,
                port=self.cfg.ch_port,
                username=self.cfg.ch_user,
                password=self.cfg.ch_password,
                database=self.cfg.ch_source_db,
            )
        return self._ch

    # ── Checkpoint store (lazy) ───────────────────────────────────────────────

    def _store(self) -> CheckpointStore:
        if not hasattr(self, "_ckpt_store"):
            self._ckpt_store = CheckpointStore(self.cfg.pg_dsn)
        return self._ckpt_store

    # ── Core operations ───────────────────────────────────────────────────────

    def fetch_batch(self, since: str, limit: int) -> list[dict]:
        """Return up to limit rows from otel_traces with Timestamp > since.

        Args:
            since: Checkpoint string 'YYYY-MM-DD HH:MM:SS.ffffff' (UTC).
            limit: Maximum number of rows to return.
        """
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d %H:%M:%S.%f").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            since_dt = datetime.strptime(since, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )

        q = f"""
            SELECT
                Timestamp,
                TraceId,
                SpanId,
                ParentSpanId,
                SpanName,
                SpanKind,
                ServiceName,
                ResourceAttributes,
                SpanAttributes,
                Duration,
                StatusCode,
                StatusMessage,
                `Events.Timestamp`,
                `Events.Name`,
                `Events.Attributes`
            FROM {self.cfg.ch_source_db}.{self.cfg.ch_source_table}
            WHERE Timestamp > %(since)s
            ORDER BY Timestamp
            LIMIT %(lim)s
        """
        res = self._ch_client().query(q, parameters={"since": since_dt, "lim": limit})
        return [dict(zip(res.column_names, row)) for row in res.result_rows]

    def write(self, rows: list[dict]) -> None:
        """Bulk-insert mapped rows into compass_raw_spans."""
        if not rows:
            return
        data = [[r.get(c) for c in RAW_COLS] for r in rows]
        self._ch_client().insert(
            f"{self.cfg.ch_dest_db}.{self.cfg.ch_dest_table}",
            data,
            column_names=RAW_COLS,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Signal the poll loop to exit cleanly after the current batch.
        Also wakes the worker immediately if it is sleeping between polls.
        """
        log.info("[etl] stop requested")
        self._stop_event.set()

    # ── Poll loop (mirrors BaseWorker.run_poll) ───────────────────────────────

    def run_poll(self, once: bool = False) -> None:
        """Production poll loop.

        Drain-when-busy, sleep-when-caught-up — identical behaviour to the
        compass-workers BaseWorker:
          * Full batch returned  → loop immediately (backlog still exists).
          * Partial / no spans   → sleep POLL_SEC before next round.
          * once=True            → process one non-empty batch and return.

        Args:
            once: If True, process one non-empty batch then exit the loop.
        """
        store      = self._store()
        batch_size = self.cfg.batch_size
        poll_sec   = self.cfg.poll_sec

        log.info(
            "[etl] starting poll loop  source=%s.%s  dest=%s.%s  batch=%d  poll_sec=%.1fs",
            self.cfg.ch_source_db, self.cfg.ch_source_table,
            self.cfg.ch_dest_db,   self.cfg.ch_dest_table,
            batch_size, poll_sec,
        )
        print(
            f"[etl] starting poll loop  "
            f"source={self.cfg.ch_source_db}.{self.cfg.ch_source_table}  "
            f"dest={self.cfg.ch_dest_db}.{self.cfg.ch_dest_table}  "
            f"batch={batch_size}  poll_sec={poll_sec}s"
        )

        while not self._stop_event.is_set():
            any_processed   = False   # tracks whether this round processed anything
            full_batch_seen = False   # if True, skip sleep and drain immediately

            # ── Load checkpoint ───────────────────────────────────────────────
            wm = store.load()

            # ── Fetch ─────────────────────────────────────────────────────────
            spans = self.fetch_batch(wm, batch_size)
            print(f"[etl] fetched {len(spans)} spans newer than {wm}")

            if not spans:
                self._stop_event.wait(poll_sec)
                continue

            any_processed = True

            if len(spans) >= batch_size:
                full_batch_seen = True

            # ── Map + Write ───────────────────────────────────────────────────
            try:
                mapped = [map_span(s) for s in spans]
                self.write(mapped)
            except Exception:
                log.exception("[etl] batch failed; process will restart")
                raise

            # ── Save checkpoint ───────────────────────────────────────────────
            newest_ts: datetime = max(s["Timestamp"] for s in spans)
            newest_str = newest_ts.strftime("%Y-%m-%d %H:%M:%S.%f")
            store.save(newest_str)

            log.info(
                "[etl] %d spans written  checkpoint → %s",
                len(spans), newest_str,
            )

            if once and any_processed:
                break

            if not full_batch_seen:
                self._stop_event.wait(poll_sec)

        log.info("[etl] poll loop exited")
