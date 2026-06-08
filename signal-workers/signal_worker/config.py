"""Worker configuration — environment driven.

Set these in the environment (or a .env loaded before calling from_env()) when running:
  CH_HOST, CH_PORT, CH_DB, CH_USER, CH_PASSWORD   -> ClickHouse (telemetry)
  PG_DSN                                            -> Postgres (config/pricing; not needed for Performance)
  WORKER_BATCH, WORKER_POLL_SEC, WORKER_STATE_DIR   -> run loop tuning
"""
import os
from dataclasses import dataclass


@dataclass
class Config:
    # ClickHouse — where spans live and metrics are written
    ch_host:     str
    ch_port:     int
    ch_db:       str
    ch_user:     str
    ch_password: str

    # Postgres — config/registry/pricing. Unused by Performance; here for later lenses.
    pg_dsn:      str

    # Run loop
    batch_size:  int
    poll_sec:    float
    state_dir:   str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            ch_host     = os.getenv("CH_HOST", "localhost"),
            ch_port     = int(os.getenv("CH_PORT", "8123")),
            ch_db       = os.getenv("CH_DB", "signal"),
            ch_user     = os.getenv("CH_USER", "default"),
            ch_password = os.getenv("CH_PASSWORD", ""),
            pg_dsn      = os.getenv("PG_DSN", "postgresql://postgres@localhost:5432/signal"),
            batch_size  = int(os.getenv("WORKER_BATCH", "5000")),
            poll_sec    = float(os.getenv("WORKER_POLL_SEC", "2.0")),
            state_dir   = os.getenv("WORKER_STATE_DIR", "./worker-state"),
        )
