"""
Severity-scoring layer on top of detection.

Detection alone only reports individual entity matches — it has no notion of
*combinations* being more dangerous than the sum of their parts ("John born
in England" is safe; "John born in London on 19 May 1985" is not).
PolicyEvaluator takes the filtered span list from PresidioEngine.detect() and
emits structured Violation objects with a severity. This is detection-only
enrichment: no anonymization, no audit logging, no text mutation.

Public API:
    PolicyEvaluator().evaluate(text, results) -> list[Violation]

Three-tier evaluation per sentence-level group:
    Tier 0 — any single direct identifier -> HIGH (or PHI near medical context)
    Tier 1 — named entity-type combinations (Sweeney-style k-anonymity rules)
    Tier 2 — cumulative quasi-identifier granularity score backstop
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple


class Severity(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PHI = "phi"


_SEVERITY_RANK: Dict[Severity, int] = {
    Severity.NONE: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.PHI: 4,
}


class ViolationKind(str, Enum):
    DIRECT_IDENTIFIER = "direct_identifier"
    QI_COMBINATION = "qi_combination"
    QI_SCORE_THRESHOLD = "qi_score_threshold"
    PHI_CONTEXT = "phi_context"


# ---------------------------------------------------------------------------
# Entity classification — mirrors entities.EntityType, scoped to this module
# since it's evaluator-specific risk categorization, not a detection concern.
# PERSON is intentionally NOT a direct identifier: bare names aren't uniquely
# identifying (millions of Sarahs exist) — it's scored as a quasi-identifier
# instead, via GranularityScorer (first-name-only vs full name).
# ---------------------------------------------------------------------------
DIRECT_IDENTIFIERS: FrozenSet[str] = frozenset({
    "EMAIL_ADDRESS", "PHONE_NUMBER", "US_SSN", "CREDIT_CARD",
    "US_DRIVER_LICENSE", "US_PASSPORT", "IP_ADDRESS", "IBAN_CODE",
    "MEDICAL_LICENSE", "MEDICAL_RECORD_NUMBER", "US_BANK_NUMBER",
    "MEDICARE_ID", "URL",
})

QUASI_IDENTIFIERS: FrozenSet[str] = frozenset({
    "PERSON", "AGE", "DATE_TIME", "DATE_OF_BIRTH", "LOCATION",
    "ZIP_CODE", "NRP", "ORG", "GENDER",
})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class Violation:
    kind: ViolationKind
    severity: Severity
    entities: List["RecognizerResult"]
    score: float
    rule_name: str
    span: Tuple[int, int]


@dataclass
class ComboRule:
    name: str
    required_types: FrozenSet[str]
    severity: Severity
    min_granularity: Dict[str, float] = field(default_factory=dict)
    requires_medical_context: bool = False


@dataclass
class EntityGroup:
    entities: List["RecognizerResult"]
    start: int
    end: int


@dataclass
class EvaluatorConfig:
    qi_score_threshold: float = 1.5
    enable_phi: bool = True
    medical_context_window: int = 200


# ---------------------------------------------------------------------------
# Granularity scoring
# ---------------------------------------------------------------------------
_DECADE_WORD_RE = re.compile(r"(?i)\b\d{1,3}0s\b|\b(?:teens|twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties)\b")
_EXACT_AGE_RE = re.compile(r"\b\d{1,3}\b")

_DATE_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_FULL_DATE_RE = re.compile(
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4}\b"
    # --- compact formats (mirror Step 1's compact date recognizer) ---
    r"|\b\d{1,2}(?:st|nd|rd|th)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\d{2,4}\b"
    r"|\b\d{1,2}[\-./](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*[\-./]\d{2,4}\b"
    r"|\b(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\b",
    re.IGNORECASE,
)
_MONTH_YEAR_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4}\b",
    re.IGNORECASE,
)
_BARE_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

_STREET_RE = re.compile(
    r"(?i)\b\d+\s+\w+.*\b(?:st|street|ave|avenue|blvd|boulevard|rd|road|"
    r"dr|drive|ln|lane|way|ct|court|pl|place)\b\.?"
)
_ZIP_SHAPED_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_COUNTRY_RE = re.compile(
    r"(?i)\b(?:usa|u\.s\.a\.?|united states|uk|u\.k\.|united kingdom|england|"
    r"scotland|wales|canada|india|china|japan|germany|france|italy|spain|"
    r"australia|brazil|mexico|russia)\b"
)
_STATE_RE = re.compile(
    r"(?i)\b(?:alabama|alaska|arizona|arkansas|california|colorado|"
    r"connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|"
    r"iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
    r"minnesota|mississippi|missouri|montana|nebraska|nevada|"
    r"new hampshire|new jersey|new mexico|new york|north carolina|"
    r"north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|"
    r"south carolina|south dakota|tennessee|texas|utah|vermont|virginia|"
    r"washington|west virginia|wisconsin|wyoming)\b"
)

_URL_PROFILE_RE = re.compile(
    r"(?i)/(?:in|user|users|profile|u|~)/[\w\-.]+|@[\w\-.]+$"
)


class GranularityScorer:
    """Scores how identifying a matched substring is, in [0, 1].

    A bare entity type is insufficient — "London" and "221B Baker St" are
    both LOCATION but very different. There is no granularity metadata on a
    Presidio match (just entity_type/start/end/score), so this is a
    text-pattern heuristic over the matched substring. Weights are
    defensible starting points, not measured against a labeled corpus.
    """

    def score(self, entity_type: str, matched_text: str) -> float:
        if entity_type in DIRECT_IDENTIFIERS:
            if entity_type == "URL":
                return 0.85 if _URL_PROFILE_RE.search(matched_text) else 0.5
            return 1.0

        method = getattr(self, f"_score_{entity_type.lower()}", None)
        if method is not None:
            return method(matched_text)
        return 0.5

    def _score_location(self, text: str) -> float:
        if _STREET_RE.search(text) or _ZIP_SHAPED_RE.search(text):
            return 1.0
        if _STATE_RE.search(text):
            return 0.3
        if _COUNTRY_RE.search(text):
            return 0.1
        return 0.6  # default: treat as city-level

    def _score_date_time(self, text: str) -> float:
        return self._score_date(text)

    def _score_date_of_birth(self, text: str) -> float:
        return self._score_date(text)

    def _score_date(self, text: str) -> float:
        if _DATE_TIME_RE.search(text):
            return 1.0
        if _FULL_DATE_RE.search(text):
            return 0.9
        if _MONTH_YEAR_RE.search(text):
            return 0.5
        if _BARE_YEAR_RE.search(text):
            return 0.2
        return 0.1

    def _score_age(self, text: str) -> float:
        if _DECADE_WORD_RE.search(text):
            return 0.2
        if _EXACT_AGE_RE.search(text):
            return 0.7
        return 0.5
    
    def _score_gender(self, text: str) -> float:
        """Gender partitions population by ~half (binary) or ~third (with NB).
        Low granularity — meaningful only in combination."""
        return 0.3

    def _score_person(self, text: str) -> float:
        tokens = [t for t in text.strip().split() if t]
        return 0.9 if len(tokens) >= 2 else 0.4

    def _score_zip_code(self, text: str) -> float:
        digits = re.sub(r"\D", "", text)
        return 0.9 if len(digits) >= 5 else 0.5

    def _score_org(self, text: str) -> float:
        return 0.5

    def _score_nrp(self, text: str) -> float:
        return 0.3


# ---------------------------------------------------------------------------
# Sentence-level grouping
# ---------------------------------------------------------------------------
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]\s+|\n{2,}")


class SentenceGrouper:
    """Groups entities by co-occurrence in the same sentence.

    Entities 400+ chars apart are probably not about the same person.
    Sentence-level grouping is a default good enough for ~80% of cases — a
    coref-aware grouper (for pronoun chains) is a later improvement.
    """

    def group(self, text: str, results: List["RecognizerResult"]) -> List[EntityGroup]:
        if not results:
            return []

        bounds: List[Tuple[int, int]] = []
        pos = 0
        for m in _SENTENCE_SPLIT_RE.finditer(text):
            bounds.append((pos, m.start()))
            pos = m.end()
        bounds.append((pos, len(text)))

        buckets: List[List["RecognizerResult"]] = [[] for _ in bounds]
        for r in sorted(results, key=lambda r: r.start):
            idx = self._find_bucket(bounds, r.start)
            buckets[idx].append(r)

        groups = []
        for (s, e), entities in zip(bounds, buckets):
            if entities:
                start = min(s, min(en.start for en in entities))
                end = max(e, max(en.end for en in entities))
                groups.append(EntityGroup(entities=entities, start=start, end=end))
        return groups

    @staticmethod
    def _find_bucket(bounds: List[Tuple[int, int]], pos: int) -> int:
        for i, (s, e) in enumerate(bounds):
            if s <= pos < e:
                return i
        return len(bounds) - 1


# ---------------------------------------------------------------------------
# Medical context
# ---------------------------------------------------------------------------
_MEDICAL_CONTEXT_RE = re.compile(
    r"(?i)\b(?:patient|diagnos\w*|prescri\w*|symptom\w*|treatment\w*|"
    r"hospital\w*|mg|ml|dose|dosage|medication\w*|cancer|diabetes|"
    r"hypertension|asthma|covid|hiv|icd-\d+|cpt-\d+|surgery|surgical|"
    r"oncolog\w*|chemo\w*|radiolog\w*|clinic\w*|physician|nurse|"
    r"pharmac\w*)\b"
)


def has_medical_context(text: str, group: EntityGroup, window: int = 200) -> bool:
    window_text = text[max(0, group.start - window): group.end + window]
    return bool(_MEDICAL_CONTEXT_RE.search(window_text))


# ---------------------------------------------------------------------------
# Combination rules (Sweeney-style k-anonymity), most-specific-first
# ---------------------------------------------------------------------------
COMBO_RULES: Tuple[ComboRule, ...] = (
    # ====================================================================
    # Tier A — 4 entities, highest specificity
    # ====================================================================

    # Hospital admission record (your scenario): male/female + age + date
    # near medical context. Severity HIGH base → auto-bumps to PHI because
    # medical context is required.
    ComboRule(
        name="age_gender_date_medical",
        required_types=frozenset({"AGE", "GENDER", "DATE_TIME"}),
        severity=Severity.HIGH,
        min_granularity={"AGE": 0.5, "DATE_TIME": 0.7},
        requires_medical_context=True,
    ),

    # ====================================================================
    # Tier B — 3 entities
    # ====================================================================

    # Sweeney's canonical re-identification triad (87% of US population
    # uniquely identifiable by these three alone — original 2000 study).
    ComboRule(
        name="sweeney_canonical",
        required_types=frozenset({"DATE_OF_BIRTH", "ZIP_CODE", "GENDER"}),
        severity=Severity.HIGH,
    ),

    # Sweeney variant: location at city/street granularity replaces ZIP.
    ComboRule(
        name="sweeney_location_gender",
        required_types=frozenset({"DATE_OF_BIRTH", "LOCATION", "GENDER"}),
        severity=Severity.HIGH,
        min_granularity={"LOCATION": 0.6},
    ),

    # Existing — legacy Sweeney variant with NRP (nationality/religion/etc).
    ComboRule(
        name="sweeney_triad",
        required_types=frozenset({"DATE_OF_BIRTH", "ZIP_CODE", "NRP"}),
        severity=Severity.HIGH,
    ),

    # Full name + DOB + city/street → very identifying.
    ComboRule(
        name="name_dob_location",
        required_types=frozenset({"PERSON", "DATE_OF_BIRTH", "LOCATION"}),
        severity=Severity.HIGH,
        min_granularity={"LOCATION": 0.6},
    ),

    # Full name + specific date + city/street → who/when/where.
    ComboRule(
        name="name_date_location",
        required_types=frozenset({"PERSON", "DATE_TIME", "LOCATION"}),
        severity=Severity.HIGH,
        min_granularity={"DATE_TIME": 0.7, "LOCATION": 0.6},
    ),

    # Existing — person + org + location.
    ComboRule(
        name="person_org_location",
        required_types=frozenset({"PERSON", "ORG", "LOCATION"}),
        severity=Severity.HIGH,
        min_granularity={"LOCATION": 0.6},
    ),

    # Existing — person + age + location.
    ComboRule(
        name="person_age_location",
        required_types=frozenset({"PERSON", "AGE", "LOCATION"}),
        severity=Severity.MEDIUM,
        min_granularity={"AGE": 0.7, "LOCATION": 0.6},
    ),

    # Sweeney-light: age + gender + ZIP (no DOB).
    ComboRule(
        name="age_gender_zip",
        required_types=frozenset({"AGE", "GENDER", "ZIP_CODE"}),
        severity=Severity.MEDIUM,
        min_granularity={"AGE": 0.7},
    ),

    # Age + gender + city-level location.
    ComboRule(
        name="age_gender_location",
        required_types=frozenset({"AGE", "GENDER", "LOCATION"}),
        severity=Severity.MEDIUM,
        min_granularity={"AGE": 0.7, "LOCATION": 0.7},
    ),

    # Existing — age + ZIP + org.
    ComboRule(
        name="age_zip_org",
        required_types=frozenset({"AGE", "ZIP_CODE", "ORG"}),
        severity=Severity.MEDIUM,
    ),

    # Specific date + org + location → "visited X clinic in Mumbai on 20 May".
    ComboRule(
        name="date_org_location",
        required_types=frozenset({"DATE_TIME", "ORG", "LOCATION"}),
        severity=Severity.MEDIUM,
        min_granularity={"DATE_TIME": 0.7, "LOCATION": 0.6},
    ),

    # ====================================================================
    # Tier C — 2 entities, least specific
    # ====================================================================

    # Existing — name + DOB direct match.
    ComboRule(
        name="name_dob",
        required_types=frozenset({"PERSON", "DATE_OF_BIRTH"}),
        severity=Severity.HIGH,
        min_granularity={"DATE_OF_BIRTH": 0.5},
    ),

    # Full name + specific ZIP → typically unique (5-digit ZIP averages
    # ~10k residents; rare full names will be singletons there).
    ComboRule(
        name="name_zip",
        required_types=frozenset({"PERSON", "ZIP_CODE"}),
        severity=Severity.HIGH,
        min_granularity={"PERSON": 0.9, "ZIP_CODE": 0.9},
    ),

    # Full name + org → identifying when name is rare within the org.
    ComboRule(
        name="name_org",
        required_types=frozenset({"PERSON", "ORG"}),
        severity=Severity.MEDIUM,
        min_granularity={"PERSON": 0.9},
    ),

    # ====================================================================
    # Tier D — medical-context-only
    # ====================================================================

    # Hospital admission without gender (your scenario, second version).
    # Bucket of N matches at one hospital on one date — re-identifiable
    # in the records context. Auto-bumps to PHI from MEDIUM via the
    # medical-context PHI rule in _evaluate_group.
    ComboRule(
        name="age_date_medical",
        required_types=frozenset({"AGE", "DATE_TIME"}),
        severity=Severity.MEDIUM,
        min_granularity={"AGE": 0.5, "DATE_TIME": 0.7},
        requires_medical_context=True,
    ),
)


# ---------------------------------------------------------------------------
# PolicyEvaluator
# ---------------------------------------------------------------------------
class PolicyEvaluator:
    def __init__(self, config: Optional[EvaluatorConfig] = None) -> None:
        self.config = config or EvaluatorConfig()
        self._granularity = GranularityScorer()
        self._grouper = SentenceGrouper()

    def evaluate(self, text: str, results: List["RecognizerResult"]) -> List[Violation]:
        violations: List[Violation] = []
        for group in self._grouper.group(text, results):
            violations.extend(self._evaluate_group(text, group))
        return violations

    def _evaluate_group(self, text: str, group: EntityGroup) -> List[Violation]:
        covered: Set[int] = set()
        out: List[Violation] = []
        medical = self.config.enable_phi and has_medical_context(
            text, group, self.config.medical_context_window
        )

        # Tier 0 — direct identifiers
        for e in group.entities:
            if e.entity_type in DIRECT_IDENTIFIERS:
                severity = Severity.PHI if medical else Severity.HIGH
                out.append(Violation(
                    kind=ViolationKind.DIRECT_IDENTIFIER,
                    severity=severity,
                    entities=[e],
                    score=1.0,
                    rule_name=f"direct:{e.entity_type}",
                    span=(e.start, e.end),
                ))
                covered.add(id(e))

        # Tier 1 — named combinations, most-specific-first
        for rule in COMBO_RULES:

            if rule.requires_medical_context and not medical:
                continue

            by_type: Dict[str, List["RecognizerResult"]] = {}
            for e in group.entities:
                if id(e) in covered:
                    continue
                by_type.setdefault(e.entity_type, []).append(e)

            if not rule.required_types.issubset(by_type.keys()):
                continue

            chosen: Dict[str, "RecognizerResult"] = {}
            satisfied = True
            for etype in rule.required_types:
                candidates = by_type[etype]
                best = max(
                    candidates,
                    key=lambda e: self._granularity.score(e.entity_type, text[e.start:e.end]),
                )
                min_req = rule.min_granularity.get(etype, 0.0)
                if self._granularity.score(best.entity_type, text[best.start:best.end]) < min_req:
                    satisfied = False
                    break
                chosen[etype] = best

            if not satisfied:
                continue

            entities = list(chosen.values())
            severity = Severity.PHI if medical else rule.severity
            out.append(Violation(
                kind=ViolationKind.QI_COMBINATION,
                severity=severity,
                entities=entities,
                score=sum(
                    self._granularity.score(e.entity_type, text[e.start:e.end])
                    for e in entities
                ),
                rule_name=rule.name,
                span=(min(e.start for e in entities), max(e.end for e in entities)),
            ))
            covered.update(id(e) for e in entities)

        # Tier 2 — cumulative quasi-identifier score backstop
        remaining = [
            e for e in group.entities
            if id(e) not in covered and e.entity_type in QUASI_IDENTIFIERS
        ]
        weights = [
            self._granularity.score(e.entity_type, text[e.start:e.end])
            for e in remaining
        ]
        total = sum(weights)
        if total >= self.config.qi_score_threshold and len(remaining) >= 2:
            severity = Severity.PHI if medical else Severity.MEDIUM
            out.append(Violation(
                kind=ViolationKind.QI_SCORE_THRESHOLD,
                severity=severity,
                entities=remaining,
                score=total,
                rule_name="qi_score_backstop",
                span=(min(e.start for e in remaining), max(e.end for e in remaining)),
            ))

        return out
