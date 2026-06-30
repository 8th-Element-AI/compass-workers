"""DETECT + CLASSIFY — turn (rule, observed value) into candidate insights.

Two modes:
  * threshold     — observed value vs the rule's warning/critical bound.
  * baseline_drift — observed value vs the trailing baseline (fractional change).

Each produces zero or more `Candidate`s (one per environment that breached),
already carrying a severity and a stable `fingerprint` used for reconciliation.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class Candidate:
    """One detected signal this tick — becomes/updates an `insights` row."""
    fingerprint: str
    lens: str
    detection_mode: str            # "threshold" | "baseline_drift"
    severity: str                  # "high" | "medium" | "low"
    scope: str
    solution_id: str
    endpoint: str
    workflow_id: str
    agent_id: str
    component_id: str
    component_type: str
    environment: str
    metric: str
    time_window: str
    operator: str | None
    observed_value: float | None
    threshold_value: float | None
    baseline_value: float | None
    deviation: float | None
    threshold_id: str | None
    details: dict = field(default_factory=dict)

    # entity path as a tuple (used by correlation for ancestry checks)
    def path(self) -> tuple[str, str, str, str, str]:
        return (
            self.solution_id, self.endpoint, self.workflow_id,
            self.agent_id, self.component_id,
        )


def fingerprint(lens, mode, scope, sol, ep, wf, ag, comp, env, metric, window) -> str:
    """Deterministic identity of a signal. Same inputs → same hash → same row."""
    raw = "|".join((lens, mode, scope, sol, ep, wf, ag, comp, env, metric, window))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _breached(operator: str, value: float, bound: float | None) -> bool:
    if bound is None:
        return False
    if operator == "gt":
        return value > bound
    if operator == "lt":
        return value < bound
    return False


def _make(rule, env: str, mode: str, severity: str, *, observed, threshold_value,
          baseline_value, deviation, details) -> Candidate:
    fp = fingerprint(
        rule.lens, mode, rule.scope, rule.solution_id, rule.endpoint,
        rule.workflow_id, rule.agent_id, rule.component_id, env,
        rule.metric, rule.time_window,
    )
    return Candidate(
        fingerprint=fp,
        lens=rule.lens,
        detection_mode=mode,
        severity=severity,
        scope=rule.scope,
        solution_id=rule.solution_id,
        endpoint=rule.endpoint,
        workflow_id=rule.workflow_id,
        agent_id=rule.agent_id,
        component_id=rule.component_id,
        component_type=rule.component_type,
        environment=env,
        metric=rule.metric,
        time_window=rule.time_window,
        operator=rule.operator,
        observed_value=observed,
        threshold_value=threshold_value,
        baseline_value=baseline_value,
        deviation=deviation,
        threshold_id=rule.threshold_id,
        details=details,
    )


def evaluate_threshold(rule, current_by_env: dict[str, dict]) -> list[Candidate]:
    """Compare the observed mean against the rule's warning/critical bounds."""
    out: list[Candidate] = []
    for env, stats in current_by_env.items():
        value = stats.get("avg")
        if value is None or stats.get("n", 0) <= 0:
            continue
        if _breached(rule.operator, value, rule.critical_value):
            sev, bound = "high", rule.critical_value
        elif _breached(rule.operator, value, rule.warning_value):
            sev, bound = "medium", rule.warning_value
        else:
            continue
        out.append(_make(
            rule, env, "threshold", sev,
            observed=value, threshold_value=bound,
            baseline_value=None, deviation=None,
            details={"n": stats.get("n"), "p95": stats.get("p95"),
                     "max": stats.get("max"), "operator": rule.operator},
        ))
    return out


def evaluate_drift(rule, current_by_env: dict[str, dict],
                   baseline_by_env: dict[str, dict],
                   cutoff: float, min_samples: int) -> list[Candidate]:
    """Flag when the current mean deviates from the trailing baseline by >= cutoff.

    deviation = (current - baseline) / |baseline|. Severity: |deviation| past
    twice the cutoff → high, else medium.
    """
    out: list[Candidate] = []
    for env, cur in current_by_env.items():
        cur_v = cur.get("avg")
        if cur_v is None or cur.get("n", 0) <= 0:
            continue
        base = baseline_by_env.get(env)
        if not base:
            continue
        base_v = base.get("avg")
        if base_v is None or base.get("n", 0) < min_samples or base_v == 0:
            continue
        deviation = (cur_v - base_v) / abs(base_v)
        if abs(deviation) < cutoff:
            continue
        sev = "high" if abs(deviation) >= 2 * cutoff else "medium"
        out.append(_make(
            rule, env, "baseline_drift", sev,
            observed=cur_v, threshold_value=None,
            baseline_value=base_v, deviation=deviation,
            details={"n": cur.get("n"), "baseline_n": base.get("n"),
                     "cutoff": cutoff, "pct_change": round(deviation * 100, 1)},
        ))
    return out
