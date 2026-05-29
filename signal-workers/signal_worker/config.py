"""Worker configuration — environment driven, with sensible local defaults.

Set these in the environment (or a .env) when running against your stack:
  CH_HOST, CH_PORT, CH_DB, CH_USER, CH_PASSWORD   -> ClickHouse (telemetry)
  PG_DSN                                            -> Postgres (config/pricing; not needed for Performance)
  WORKER_BATCH, WORKER_POLL_SEC, WORKER_STATE_DIR   -> run loop tuning
"""
import os
from dataclasses import dataclass


@dataclass
class Config:
    # ClickHouse — where spans live and metrics are written
    ch_host: str = os.getenv("CH_HOST", "localhost")
    ch_port: int = int(os.getenv("CH_PORT", "8123"))   # HTTP interface
    ch_db: str = os.getenv("CH_DB", "signal")
    ch_user: str = os.getenv("CH_USER", "default")
    ch_password: str = os.getenv("CH_PASSWORD", "password")

    # Postgres — config/registry/pricing. Unused by Performance; here for later lenses.
    pg_dsn: str = os.getenv("PG_DSN", "postgresql://postgres:dev@localhost:5433/signal")

    # Run loop
    batch_size: int = int(os.getenv("WORKER_BATCH", "5000"))
    poll_sec: float = float(os.getenv("WORKER_POLL_SEC", "2.0"))
    state_dir: str = os.getenv("WORKER_STATE_DIR", "E:/8thelement/Signal/state-dir")

    @classmethod
    def from_env(cls) -> "Config":
        return cls()
