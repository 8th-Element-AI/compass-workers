"""Public surface of the quality-observability runtime.

Signal Workers imports `LocalScorer` from here; the Quality lens does
nothing more than build a config_dict and pass it in. The interface
`QualityScorer` plus the deterministic `StaticScorer` allow tests and
offline `--csv` runs to skip torch entirely.
"""
from __future__ import annotations

from .pipeline import LocalScorer
from .scorer import QualityScorer, StaticScorer

__all__ = ["LocalScorer", "QualityScorer", "StaticScorer"]