"""Worker configuration — environment driven, with sensible local defaults.

Set these in the environment (or a .env) when running against your stack:
  CH_HOST, CH_PORT, CH_DB, CH_USER, CH_PASSWORD   -> ClickHouse (telemetry)
  PG_DSN                                            -> Postgres (config/pricing; not needed for Performance)
  WORKER_BATCH, WORKER_POLL_SEC, WORKER_STATE_DIR   -> run loop tuning
"""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    ch_host: str = Field(default="localhost", alias="CH_HOST")
    ch_port: int = Field(default=8123, alias="CH_PORT")
    ch_db: str = Field(default="signal", alias="CH_DB")
    ch_user: str = Field(
      default="default",
      alias="CH_USER"
    )

    ch_password: str = Field(
      default="",
      alias="CH_PASSWORD"
    )

    pg_dsn: str = Field(
      default="",
      alias="PG_DSN"
    )

    batch_size: int = Field(
      default=5000,
      alias="WORKER_BATCH"
    )

    poll_sec: float = Field(
      default=2.0,
      alias="WORKER_POLL_SEC"
    )

    signal_toggle_ttl: float = Field(
      default=300,
      alias="SIGNAL_TOGGLE_TTL"
    )

    signal_pii_ner_model: str = Field(
      default="gravitee-io/bert-small-pii-detection",
      alias="SIGNAL_PII_NER_MODEL"
    )

    signal_pii_batch_size: int = Field(
      default=4,
      alias="SIGNAL_PII_BATCH_SIZE"
    )

    signal_pii_cache_max: int = Field(
      default=20000,
      alias="SIGNAL_PII_CACHE_MAX"
    )

    signal_toxicity_cache_max: int = Field(
      default=20000,
      alias="SIGNAL_TOXICITY_CACHE_MAX"
    )

    signal_toxicity_batch_size: int = Field(
      default=64,
      alias="SIGNAL_TOXICITY_BATCH_SIZE"
    )

    # ── Toxicity — device + perf ────────────────────────────────
    signal_toxicity_device: str = Field(
        default="cpu", alias="SIGNAL_TOXICITY_DEVICE"
    )  # "cuda" | "cpu"
    signal_toxicity_max_length: int = Field(
        default=512, alias="SIGNAL_TOXICITY_MAX_LENGTH"
    )
    signal_toxicity_fp16: bool = Field(
        default=True, alias="SIGNAL_TOXICITY_FP16"
    )

    # ── Toxicity — model paths ─────────────────────────────────
    signal_toxicity_pi_path: str = Field(
        default="prompt_injection",
        alias="SIGNAL_TOXICITY_PI_PATH",
    )
    signal_toxicity_mod_path: str = Field(
        default="minilm_toxic_spam",
        alias="SIGNAL_TOXICITY_MOD_PATH",
    )

    # ── Toxicity — review thresholds (drive 0/1 verdicts) ──────
    signal_toxicity_pi_threshold: float = Field(
        default=0.50, alias="SIGNAL_TOXICITY_PI_THRESHOLD"
    )
    signal_toxicity_harmful_threshold: float = Field(
        default=0.50, alias="SIGNAL_TOXICITY_HARMFUL_THRESHOLD"
    )