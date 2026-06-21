"""YAML config loader + path resolution.

Identical shape to `toxicity_observability.config`: a `load_config()` that
reads `configs/runtime.yaml` by default, and a `resolve_path()` that turns
a YAML-relative `local_path` into an absolute Path under the package root.

Why the helper exists: model artifacts are referenced in the YAML as
relative paths (e.g. `models/nli`). At install time, those resolve under
the `quality/` repo. In a baked Docker image, they typically resolve
under `/opt/models/<name>` — set via absolute paths in the image's
runtime.yaml, no code change needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def repo_root() -> Path:
    """Package repo root — one level above this file."""
    return Path(__file__).resolve().parents[1]


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load runtime.yaml as a dict.

    Args:
        config_path: Explicit path. None -> defaults to <repo_root>/configs/runtime.yaml.

    Returns:
        Parsed YAML as nested dict. No defaulting / validation happens here —
        the LocalScorer applies its own per-section defaults so a partial
        config (e.g. config_dict from compass-workers that omits `recipes:`)
        still works.
    """
    cfg_path = Path(config_path) if config_path else repo_root() / "configs" / "runtime.yaml"
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def resolve_path(path: str | Path) -> Path:
    """Resolve a YAML-relative path to an absolute Path under the repo root.

    Absolute paths pass through unchanged — that's how the production
    runtime.yaml (inside the Docker image) points at `/opt/models/nli` etc.
    """
    p = Path(path)
    return p if p.is_absolute() else repo_root() / p