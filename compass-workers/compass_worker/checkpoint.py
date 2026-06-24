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

log = logging.getLogger("compass.worker.checkpoint")

DEFAULT_CHECKPOINT = "1970-01-01 00:00:00.000"


class PostgresCheckpointStore:
    """Postgres-backed checkpoint store.

    One row per (lens, partition_key) in worker_checkpoints. Each save is
    an atomic UPSERT. For unpartitioned lenses, partition_key="default";
    for partitioned lenses, partition_key="slot:<n>" — see
    compass_worker.partition.slot_partition_key.
    """

    def __init__(self, dsn: str, updated_by: str | None = None):
        self.dsn = dsn
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

    def load(self, lens: str, partition_key: str = "default") -> str:
        with self._lock:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT checkpoint FROM worker_checkpoints "
                    "WHERE lens = %s AND partition_key = %s",
                    (lens, partition_key),
                )
                row = cur.fetchone()
                if row is None:
                    log.info(
                        "[checkpoint] %s/%s: no row — starting from %s",
                        lens, partition_key, DEFAULT_CHECKPOINT,
                    )
                    return DEFAULT_CHECKPOINT
                return row[0]

    def save(
        self,
        lens: str,
        checkpoint: str,
        partition_key: str = "default",
    ) -> None:
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
                    (lens, partition_key, checkpoint, self.updated_by),
                )