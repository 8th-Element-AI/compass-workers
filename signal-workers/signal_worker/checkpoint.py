"""Worker checkpoint storage — Postgres only.

A worker's checkpoint is the high-water-mark `recorded_at` of the last span
it successfully processed. On restart it resumes from there.

One row per (lens, partition_key) in the `worker_checkpoints` table, written
with a single atomic UPSERT. `partition_key` is reserved for future
partitioned consumption — today it's always 'default'.

Schema (created by infra/postgres/init/03_worker_checkpoints.sql):
  CREATE TABLE worker_checkpoints (
    lens          TEXT        NOT NULL,
    partition_key TEXT        NOT NULL DEFAULT 'default',
    checkpoint     TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT,
    PRIMARY KEY (lens, partition_key)
  );
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("signal.worker.checkpoint")

DEFAULT_WATERMARK = "1970-01-01 00:00:00.000"


class PostgresCheckpointStore:
    """One row per (lens, partition_key). Atomic UPSERT per save.

    Connection is opened lazily on first call and held for the worker's
    lifetime. Save failures propagate — the run loop will crash, the pod
    will restart, and load() will pick up the last successfully-written
    checkpoint. That's what we want.
    """

    # Future: pod-specific partitioning. Today: always 'default'.
    PARTITION_KEY = "default"

    def __init__(self, dsn: str, updated_by: str | None = None):
        self.dsn = dsn
        # Diagnostic: lets us see which pod wrote last.
        self.updated_by = updated_by or os.environ.get("HOSTNAME") or "unknown"
        self._conn = None
        self._lock = threading.Lock()

    def _connection(self):
        if self._conn is None or self._conn.closed:
            import psycopg
            self._conn = psycopg.connect(self.dsn, autocommit=True)
            log.info(
                "[checkpoint] connected as updated_by=%s",
                self.updated_by,
            )
        return self._conn

    def load(self, lens: str) -> str:
        with self._lock:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT checkpoint FROM worker_checkpoints "
                    "WHERE lens = %s AND partition_key = %s",
                    (lens, self.PARTITION_KEY),
                )
                row = cur.fetchone()
                if row is None:
                    log.info(
                        "[checkpoint] %s: no row — starting from %s",
                        lens, DEFAULT_WATERMARK,
                    )
                    return DEFAULT_WATERMARK
                return row[0]

    def save(self, lens: str, checkpoint: str) -> None:
        with self._lock:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO worker_checkpoints
                        (lens, partition_key, checkpoint, updated_by)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (lens, partition_key) DO UPDATE
                       SET checkpoint  = EXCLUDED.checkpoint,
                           updated_at = now(),
                           updated_by = EXCLUDED.updated_by
                    """,
                    (lens, self.PARTITION_KEY, checkpoint, self.updated_by),
                )