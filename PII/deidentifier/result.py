from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .policy_evaluator import Severity, Violation


@dataclass
class AnalysisResult:
    """Lightweight result for observability.

    Detection is filtered through the severity evaluator: only entities
    contributing to a violation of severity >= MEDIUM are counted in
    `entity_count` / `entities` / `has_pii`. This means a bare AGE or
    GENDER detection — meaningless alone for re-identification — does NOT
    fire `has_pii`. The Sweeney-style combo rules + medical-context bumps
    in PolicyEvaluator determine which detections matter.

    For debugging or downstream tooling that needs the raw detection
    compass, `raw_entity_count` and `raw_entities` carry the unfiltered
    counts.

    Backwards compatibility: existing callers reading
    has_pii/entity_count/entities continue to work — the field shapes
    are unchanged; only their *meaning* shifted from "raw detection"
    to "real risk."
    """
    # Risk-aware fields — what the Safety worker reads
    has_pii: bool
    entity_count: int
    entities: Dict[str, int]   # entity_type → count, filtered to risky

    # Severity layer — additive, defaults preserve backwards compat
    severity: Severity = Severity.NONE
    has_violation: bool = False
    violations: List[Violation] = field(default_factory=list)

    # Raw detection layer — additive, for debugging / inspection
    raw_entity_count: int = 0
    raw_entities: Dict[str, int] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    """Severity-scored detection output — see policy_evaluator.PolicyEvaluator."""
    violations: List[Violation]
    max_severity: Severity
    has_violation: bool