-- ─────────────────────────────────────────────────────────────────────
-- worker_checkpoints — high-watermark per (lens, partition_key)
-- ─────────────────────────────────────────────────────────────────────
-- Replaces the file-based checkpoint. Required for K8s deployments that
-- run more than one replica of a lens. Today partition_key is always
-- 'default'; the column exists so future partitioned consumption can
-- land without a schema migration.

CREATE TABLE IF NOT EXISTS worker_checkpoints (
    lens          TEXT        NOT NULL,
    partition_key TEXT        NOT NULL DEFAULT 'default',
    checkpoint     TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT,
    PRIMARY KEY (lens, partition_key)
);

COMMENT ON TABLE  worker_checkpoints              IS 'High-watermark per (lens, partition). Updated atomically each worker batch.';
COMMENT ON COLUMN worker_checkpoints.lens         IS 'Lens name: performance | cost | safety | ...';
COMMENT ON COLUMN worker_checkpoints.partition_key IS 'Future: hash-partitioned consumption. Today always ''default''.';
COMMENT ON COLUMN worker_checkpoints.checkpoint    IS 'Last successfully-processed recorded_at, as ''YYYY-MM-DD HH:MM:SS.mmm''.';
COMMENT ON COLUMN worker_checkpoints.updated_by   IS 'HOSTNAME of the writer pod. Diagnostic only.';