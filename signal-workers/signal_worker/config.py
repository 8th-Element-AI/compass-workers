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
      default=32,
      alias="SIGNAL_TOXICITY_BATCH_SIZE"
    )

    # ── Toxicity — device + perf ────────────────────────────────
    signal_toxicity_device: str = Field(
        default="cuda", alias="SIGNAL_TOXICITY_DEVICE"
    )  # "cuda" | "cpu"
    signal_toxicity_max_length: int = Field(
        default=128, alias="SIGNAL_TOXICITY_MAX_LENGTH"
    )
    signal_toxicity_fp16: bool = Field(
        default=True, alias="SIGNAL_TOXICITY_FP16"
    )

    # ── Toxicity — model paths ─────────────────────────────────
    # Paths can be absolute, OR relative — relatives are joined to
    # SIGNAL_TOXICITY_MODELS_ROOT. Useful when a container mounts a
    # PVC at /opt/models: set the root once, don't touch the four below.
    signal_toxicity_models_root: str = Field(
        default="./models", alias="SIGNAL_TOXICITY_MODELS_ROOT"
    )
    signal_toxicity_fasttext_path: str = Field(
        default="fasttext/router_head.ftz",
        alias="SIGNAL_TOXICITY_FASTTEXT_PATH",
    )
    signal_toxicity_pi_path: str = Field(
        default="transformers/prompt_injection",
        alias="SIGNAL_TOXICITY_PI_PATH",
    )
    signal_toxicity_pi_onnx_path: str = Field(
        default="onnx_int8/prompt_injection",
        alias="SIGNAL_TOXICITY_PI_ONNX_PATH",
    )
    signal_toxicity_mod_path: str = Field(
        default="transformers/moderation",
        alias="SIGNAL_TOXICITY_MOD_PATH",
    )

    # ── Toxicity — FastText routing thresholds ─────────────────
    signal_toxicity_attack_route: float = Field(
        default=0.05, alias="SIGNAL_TOXICITY_ATTACK_ROUTE"
    )
    signal_toxicity_moderation_route: float = Field(
        default=0.05, alias="SIGNAL_TOXICITY_MODERATION_ROUTE"
    )
    signal_toxicity_fast_allow: float = Field(
        default=0.02, alias="SIGNAL_TOXICITY_FAST_ALLOW"
    )
    signal_toxicity_fasttext_direct: float = Field(
        default=0.97, alias="SIGNAL_TOXICITY_FASTTEXT_DIRECT"
    )

    # ── Toxicity — BERT review thresholds (drive 0/1 verdicts) ─
    signal_toxicity_pi_threshold: float = Field(
        default=0.50, alias="SIGNAL_TOXICITY_PI_THRESHOLD"
    )
    signal_toxicity_harmful_threshold: float = Field(
        default=0.50, alias="SIGNAL_TOXICITY_HARMFUL_THRESHOLD"
    )
    signal_toxicity_sexual_threshold: float = Field(
        default=0.50, alias="SIGNAL_TOXICITY_SEXUAL_THRESHOLD"
    )