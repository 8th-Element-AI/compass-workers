"""CORRELATE — group this tick's candidates into incidents.

Within the same (solution_id, environment), candidates whose entity paths are on
the same lineage (one is an ancestor of the other on the materialized path) are
treated as symptoms of one underlying problem and bundled into an incident with
a confidence score. Lone candidates (no relatives) get no incident.

v1 simplifications (see docs/insights.md):
  * grouping is by path ancestry only (temporal overlap is implicit — all
    candidates are from the same tick; metric co-movement is not yet weighed).
  * confidence = 0.55 + 0.15*(members-1), capped at 0.95.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .detect import Candidate

_SEV_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass
class Incident:
    fingerprint: str
    solution_id: str
    environment: str
    severity: str
    root_scope: str
    root_entity: str
    confidence: float
    members: list[Candidate] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def _filled(path: tuple[str, ...]) -> tuple[str, ...]:
    """Path truncated to its last non-empty level (blanks only ever trail)."""
    last = -1
    for i, v in enumerate(path):
        if v:
            last = i
    return path[: last + 1]


def _is_lineage(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True if a and b are on the same lineage (one is a prefix of the other)."""
    fa, fb = _filled(a), _filled(b)
    short, long_ = (fa, fb) if len(fa) <= len(fb) else (fb, fa)
    return long_[: len(short)] == short


def _components(cands: list[Candidate]) -> list[list[Candidate]]:
    """Connected components where edges = same-lineage pairs (union-find)."""
    parent = list(range(len(cands)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            if _is_lineage(cands[i].path(), cands[j].path()):
                union(i, j)

    groups: dict[int, list[Candidate]] = {}
    for i, c in enumerate(cands):
        groups.setdefault(find(i), []).append(c)
    return list(groups.values())


def correlate(candidates: list[Candidate]) -> list[Incident]:
    """Return incidents (clusters of 2+ lineage-related candidates)."""
    # bucket by (solution, environment) first — never correlate across them
    buckets: dict[tuple[str, str], list[Candidate]] = {}
    for c in candidates:
        buckets.setdefault((c.solution_id, c.environment), []).append(c)

    incidents: list[Incident] = []
    for (sol, env), group in buckets.items():
        for members in _components(group):
            if len(members) < 2:
                continue
            # root = deepest member on the lineage (most specific = likely cause)
            root = max(members, key=lambda c: len(_filled(c.path())))
            root_filled = _filled(root.path())
            root_entity = root_filled[-1] if root_filled else sol
            severity = max((m.severity for m in members), key=lambda s: _SEV_RANK[s])
            confidence = min(0.95, 0.55 + 0.15 * (len(members) - 1))
            # Fingerprint on the root's FULL path (not just its id): the same
            # component can be the root of distinct lineage clusters (under
            # different agents), so the bare id would collide within one tick.
            root_path = "/".join(root.path())
            fp = hashlib.sha256(
                "|".join(("incident", sol, env, root.scope, root_path)).encode()
            ).hexdigest()[:32]
            incidents.append(Incident(
                fingerprint=fp,
                solution_id=sol,
                environment=env,
                severity=severity,
                root_scope=root.scope,
                root_entity=root_entity,
                confidence=confidence,
                members=members,
                details={
                    "member_metrics": sorted({m.metric for m in members}),
                    "member_count": len(members),
                },
            ))
    return incidents
