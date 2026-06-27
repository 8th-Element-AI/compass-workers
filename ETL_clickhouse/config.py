"""ETL worker configuration — environment driven.

Set these in the environment or a .env file next to this package:

  CH_HOST, CH_PORT, CH_USER, CH_PASSWORD  -> ClickHouse connection
  CH_SOURCE_DB, CH_SOURCE_TABLE           -> where OTel traces land (default.otel_traces)
  CH_DEST_DB, CH_DEST_TABLE               -> where Compass raw spans go (compass.compass_raw_spans)
  PG_DSN                                  -> Postgres for checkpoint storage
  BATCH_SIZE, POLL_SEC                    -> run-loop tuning
"""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── ClickHouse connection ─────────────────────────────────────────────────
    ch_host: str = Field(default="localhost", alias="CH_HOST")
    ch_port: int = Field(default=8123,        alias="CH_PORT")
    ch_user: str = Field(default="default",   alias="CH_USER")
    ch_password: str = Field(default="",      alias="CH_PASSWORD")

    # ── Source: OTel Collector writes here by default ─────────────────────────
    ch_source_db: str    = Field(default="default",     alias="CH_SOURCE_DB")
    ch_source_table: str = Field(default="otel_traces", alias="CH_SOURCE_TABLE")

    # ── Destination: Compass raw spans table ──────────────────────────────────
    ch_dest_db: str    = Field(default="compass",            alias="CH_DEST_DB")
    ch_dest_table: str = Field(default="compass_raw_spans",  alias="CH_DEST_TABLE")

    # ── Postgres — reuses the same worker_checkpoints table as compass-workers ─
    pg_dsn: str = Field(default="postgresql://postgres:@localhost:5433/compass", alias="PG_DSN")

    # ── Run-loop ──────────────────────────────────────────────────────────────
    batch_size: int   = Field(default=5000, alias="BATCH_SIZE")
    poll_sec: float   = Field(default=5.0,  alias="POLL_SEC")
