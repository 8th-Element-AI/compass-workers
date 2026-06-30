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
    ch_db: str = Field(default="compass", alias="CH_DB")
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

    batch_size_performance: int | None = Field(default=None, alias="WORKER_BATCH_PERFORMANCE")
    batch_size_cost:        int | None = Field(default=None, alias="WORKER_BATCH_COST")
    batch_size_safety:      int | None = Field(default=None, alias="WORKER_BATCH_SAFETY")
    batch_size_quality:     int | None = Field(default=None, alias="WORKER_BATCH_QUALITY")
    batch_size_reconciler:  int | None = Field(default=None, alias="WORKER_BATCH_RECONCILER")
 
    poll_sec_performance: float | None = Field(default=None, alias="WORKER_POLL_SEC_PERFORMANCE")
    poll_sec_cost:        float | None = Field(default=None, alias="WORKER_POLL_SEC_COST")
    poll_sec_safety:      float | None = Field(default=None, alias="WORKER_POLL_SEC_SAFETY")
    poll_sec_quality:     float | None = Field(default=None, alias="WORKER_POLL_SEC_QUALITY")
    poll_sec_reconciler:  float | None = Field(default=None, alias="WORKER_POLL_SEC_RECONCILER")

    # ─── Horizontal scaling (Phase 4.3) ──────────────────────────────
    worker_partition_index: int = Field(
        default=0,
        alias="WORKER_PARTITION_INDEX",
    )
    worker_partition_count: int = Field(
        default=1,
        alias="WORKER_PARTITION_COUNT",
    )
    worker_partition_total_slots: int = Field(
        default=16,
        alias="WORKER_PARTITION_TOTAL_SLOTS",
    )

    # ─── Observability ──────────────────────────────────────
    observability_port: int = Field(
        default=8080,
        alias="OBSERVABILITY_PORT",
    )

    compass_toggle_ttl: float = Field(
      default=300,
      alias="COMPASS_TOGGLE_TTL"
    )

    compass_pii_ner_model: str = Field(
      default="gravitee-io/bert-small-pii-detection",
      alias="COMPASS_PII_NER_MODEL"
    )

    compass_pii_batch_size: int = Field(
      default=4,
      alias="COMPASS_PII_BATCH_SIZE"
    )

    compass_pii_cache_max: int = Field(
      default=20000,
      alias="COMPASS_PII_CACHE_MAX"
    )

    compass_toxicity_cache_max: int = Field(
      default=20000,
      alias="COMPASS_TOXICITY_CACHE_MAX"
    )

    compass_toxicity_batch_size: int = Field(
      default=32,
      alias="COMPASS_TOXICITY_BATCH_SIZE"
    )

    # ── Toxicity — device + perf ────────────────────────────────
    compass_toxicity_device: str = Field(
        default="cuda", alias="COMPASS_TOXICITY_DEVICE"
    )  # "cuda" | "cpu"
 
    # ── Toxicity — ONNX provider for the MiniLM moderation model ────
    # "auto" (prefer CUDA when onnxruntime-gpu present, else CPU),
    # "cpu", or "cuda".
    compass_toxicity_onnx_provider: str = Field(
        default="auto", alias="COMPASS_TOXICITY_ONNX_PROVIDER"
    )
 
    compass_toxicity_max_length: int = Field(
        default=512, alias="COMPASS_TOXICITY_MAX_LENGTH"
    )
    compass_toxicity_fp16: bool = Field(
        default=True, alias="COMPASS_TOXICITY_FP16"
    )

    # ── Toxicity — model paths ─────────────────────────────────
    # Paths can be absolute, OR relative — relatives are joined to
    # COMPASS_TOXICITY_MODELS_ROOT. Useful when a container mounts a
    # PVC at /opt/models: set the root once, don't touch the four below.
    compass_toxicity_models_root: str = Field(
        default="./models", alias="COMPASS_TOXICITY_MODELS_ROOT"
    )
    compass_toxicity_pi_path: str = Field(
        default="prompt_injection",
        alias="COMPASS_TOXICITY_PI_PATH",
    )
    compass_toxicity_mod_path: str = Field(
        default="minilm_toxic_spam",
        alias="COMPASS_TOXICITY_MOD_PATH",
    )
 
    # ── Toxicity — review thresholds (drive 0/1 verdicts) ─────
    compass_toxicity_pi_threshold: float = Field(
        default=0.50, alias="COMPASS_TOXICITY_PI_THRESHOLD"
    )
    compass_toxicity_harmful_threshold: float = Field(
        default=0.83, alias="COMPASS_TOXICITY_HARMFUL_THRESHOLD"
    )

    # ── Quality — gate + sampling ────────────────────────────
    compass_quality_semantic: bool = Field(
        default=True, alias="COMPASS_QUALITY_SEMANTIC"
    )
    compass_quality_sample: float = Field(
        default=1.0, alias="COMPASS_QUALITY_SAMPLE"
    )
    compass_quality_cache_max: int = Field(
        default=20000, alias="COMPASS_QUALITY_CACHE_MAX"
    )

    # ── Quality — runtime ────────────────────────────────────
    compass_quality_device: str = Field(
        default="cpu", alias="COMPASS_QUALITY_DEVICE"
    )
    compass_quality_batch: int = Field(
        default=32, alias="COMPASS_QUALITY_BATCH"
    )

    # ── Quality — model paths ────────────────────────────────
    # Same pattern as COMPASS_TOXICITY_MODELS_ROOT + the per-model paths:
    # paths can be absolute, or relative under the models root. The image
    # bakes models at /opt/models/<name>; dev points at ./models/<name>.
    compass_quality_models_root: str = Field(
        default="./models", alias="COMPASS_QUALITY_MODELS_ROOT"
    )
    compass_quality_nli_path: str = Field(
        default="nli", alias="COMPASS_QUALITY_NLI_PATH"
    )
    compass_quality_embed_path: str = Field(
        default="embedding", alias="COMPASS_QUALITY_EMBED_PATH"
    )
    compass_quality_relevance_path: str = Field(
        default="relevance", alias="COMPASS_QUALITY_RELEVANCE_PATH"
    )

    # ── Quality — recipe knobs (overridable; defaults match constants.py) ──
    compass_quality_premise_max_chars: int = Field(
        default=2000, alias="COMPASS_QUALITY_PREMISE_MAX_CHARS"
    )
    compass_quality_max_sents: int = Field(
        default=10, alias="COMPASS_QUALITY_MAX_SENTS"
    )
    compass_quality_sent_min_chars: int = Field(
        default=3, alias="COMPASS_QUALITY_SENT_MIN_CHARS"
    )
    compass_quality_chunk_used_cos: float = Field(
        default=0.5, alias="COMPASS_QUALITY_CHUNK_USED_COS"
    )


    # ── Insights Engine ──────────────────────────────────────
    # The engine is interval-driven (evaluates current state), not span-fetch.
    insights_tick_sec: float = Field(
        default=45.0, alias="COMPASS_INSIGHTS_TICK_SEC"
    )  # how often to re-evaluate; ~30-60s matches the 1-min metric grain
    insights_baseline_days: int = Field(
        default=7, alias="COMPASS_INSIGHTS_BASELINE_DAYS"
    )  # drift baseline lookback (hardcoded default; see docs/insights.md)
    insights_drift_enabled: bool = Field(
        default=True, alias="COMPASS_INSIGHTS_DRIFT_ENABLED"
    )
    insights_drift_cutoff: float = Field(
        default=0.5, alias="COMPASS_INSIGHTS_DRIFT_CUTOFF"
    )  # |current-baseline|/baseline >= cutoff → drift insight (0.5 = 50%)
    insights_drift_min_samples: int = Field(
        default=20, alias="COMPASS_INSIGHTS_DRIFT_MIN_SAMPLES"
    )  # need enough baseline data points before trusting drift
    insights_llm_enabled: bool = Field(
        default=False, alias="COMPASS_INSIGHTS_LLM_ENABLED"
    )  # optional Claude enrichment layer (off by default)
    insights_agg_table: str = Field(
        default="compass_aggregated_metrics", alias="COMPASS_INSIGHTS_AGG_TABLE"
    )  # aggregated-metrics table name (override for the signal_* prefix / migrations)

    def batch_for(self, lens: str) -> int:
        """Per-lens batch_size with fallback to the global WORKER_BATCH.
 
        Used by BaseWorker.run_poll. Reading via getattr-with-default means
        adding a new lens doesn't require touching this method — just add a
        `batch_size_<lens>` field above.
        """
        v = getattr(self, f"batch_size_{lens}", None)
        return v if v is not None else self.batch_size
 
    def poll_sec_for(self, lens: str) -> float:
        """Per-lens poll_sec with fallback to the global WORKER_POLL_SEC.
 
        Same fallback shape as batch_for. Returns the IDLE sleep duration,
        not a fetch interval — drain mode (full batch) bypasses this entirely.
        """
        v = getattr(self, f"poll_sec_{lens}", None)
        return v if v is not None else self.poll_sec