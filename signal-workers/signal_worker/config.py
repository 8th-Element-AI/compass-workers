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

    state_dir: str = Field(
      default="./worker_state", 
      alias="WORKER_STATE_DIR"
    )

    signal_pii_ner_model: str = Field(
      default="gravitee-io/bert-small-pii-detection",
      alias="SIGNAL_PII_NER_MODEL"
    )

    signal_pii_batch: int = Field(
      default=4,
      alias="SIGNAL_PII_BATCH"
    )

    signal_pii_cache_max: int = Field(
      default=20000,
      alias="SIGNAL_PII_CACHE_MAX"
    )

    # ---- quality lens (local-model scoring; see signal_worker/scorers.py) ----
    signal_quality_semantic: bool = Field(
      default=True,
      alias="SIGNAL_QUALITY_SEMANTIC"
    )

    signal_quality_sample: float = Field(
      default=1.0,
      alias="SIGNAL_QUALITY_SAMPLE"
    )

    # FEVER+ANLI fact-verification NLI — benchmarked at AUROC 0.739 on HaluEval QA
    # vs 0.406 for the generic cross-encoder/nli-deberta-v3-xsmall it replaced
    # (see docs/QUALITY_BENCHMARK_RESULTS.md). Faithfulness *is* fact verification.
    signal_quality_nli_model: str = Field(
      default="MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
      alias="SIGNAL_QUALITY_NLI_MODEL"
    )

    signal_quality_embed_model: str = Field(
      default="sentence-transformers/all-MiniLM-L6-v2",
      alias="SIGNAL_QUALITY_EMBED_MODEL"
    )

    signal_quality_relevance_model: str = Field(
      default="cross-encoder/ms-marco-MiniLM-L-6-v2",
      alias="SIGNAL_QUALITY_RELEVANCE_MODEL"
    )

    signal_quality_batch: int = Field(
      default=32,
      alias="SIGNAL_QUALITY_BATCH"
    )

    signal_quality_cache_max: int = Field(
      default=20000,
      alias="SIGNAL_QUALITY_CACHE_MAX"
    )