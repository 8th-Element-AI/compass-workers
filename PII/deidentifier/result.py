from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .policy_evaluator import Severity, Violation


@dataclass
class AnalysisResult:
    """Lightweight result for observability — detection only, no text replacement."""
    has_pii: bool
    entity_count: int
    entities: Dict[str, int]   # entity_type → occurrence count


@dataclass
class EvaluationResult:
    """Severity-scored detection output — see policy_evaluator.PolicyEvaluator."""
    violations: List[Violation]
    max_severity: Severity
    has_violation: bool
