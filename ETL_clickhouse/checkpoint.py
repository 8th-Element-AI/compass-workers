"""Checkpoint storage for the ETL worker.

Reuses the existing worker_checkpoints Postgres table (created by
infra/postgres/init/03_worker_checkpoints.sql) with lens='etl_clickhouse'.

The checkpoint value is the Timestamp of the last otel_traces span that was
successfully written to compass_raw_spans, stored as a microsecond-precision
string: 'YYYY-MM-DD HH:MM:SS.ffffff'.  On restart the worker fetches spans
with Timestamp strictly greater than this value.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("etl.checkpoint")

LENS = "etl_clickhouse"
DEFAULT_CHECKPOINT = "1970-01-01 00:00:00.000000"


class CheckpointStore:
    """Thread-safe Postgres-backed checkpoint store for the ETL worker."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn = None
        self._lock = threading.Lock()

    def _connection(self):
        if self._conn is None or self._conn.closed:
            import psycopg
            self._conn = psycopg.connect(self.dsn, autocommit=True)
            log.info("[checkpoint] connected to Postgres")
        return self._conn

    def load(self) -> str:
        """Return the saved checkpoint string, or DEFAULT_CHECKPOINT if none exists."""
        with self._lock:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT checkpoint FROM worker_checkpoints "
                    "WHERE lens = %s AND partition_key = 'default'",
                    (LENS,),
                )
                row = cur.fetchone()
                if row is None:
                    log.info("[checkpoint] no row found — starting from epoch")
                    return DEFAULT_CHECKPOINT
                log.info("[checkpoint] loaded %s", row[0])
                return row[0]

    def save(self, checkpoint: str) -> None:
        """Atomically upsert the checkpoint value."""
        with self._lock:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO worker_checkpoints (lens, partition_key, checkpoint)
                    VALUES (%s, 'default', %s)
                    ON CONFLICT (lens, partition_key) DO UPDATE
                        SET checkpoint = EXCLUDED.checkpoint,
                            updated_at = now()
                    """,
                    (LENS, checkpoint),
                )
            log.debug("[checkpoint] saved %s", checkpoint)
