"""Step 3 — expanded combination rules in PolicyEvaluator.

Tests use mock RecognizerResult objects so they don't depend on Presidio
installation. The evaluator only reads entity_type/start/end/score.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from deidentifier.policy_evaluator import (
    COMBO_RULES,
    PolicyEvaluator,
    Severity,
    ViolationKind,
)


@dataclass
class MockResult:
    entity_type: str
    start: int
    end: int
    score: float = 0.9


@pytest.fixture
def evaluator():
    return PolicyEvaluator()


# ------------------------------------------------------------------
# Your hospital scenarios
# ------------------------------------------------------------------

def test_hospital_scenario_with_gender_is_phi(evaluator):
    """'A man aged 42 years joined a hospital on 20 May 2024'
    Expected: age_gender_date_medical fires → severity PHI."""
    text = "A man aged 42 years joined a hospital on 20 May 2024"
    results = [
        MockResult("GENDER",    2,  5),    # "man"
        MockResult("AGE",       6, 18),    # "aged 42 years"
        MockResult("DATE_TIME", 41, 52),   # "20 May 2024"
    ]
    violations = evaluator.evaluate(text, results)
    assert violations, "expected at least one violation"
    assert violations[0].rule_name == "age_gender_date_medical"
    assert violations[0].severity == Severity.PHI


def test_hospital_scenario_no_gender_still_phi(evaluator):
    """'Patient aged 42 admitted on 20 May 2024'
    Expected: age_date_medical fires → MEDIUM base, PHI from medical bump."""
    text = "Patient aged 42 admitted on 20 May 2024"
    results = [
        MockResult("AGE",       8, 14),    # "aged 42"
        MockResult("DATE_TIME", 28, 39),   # "20 May 2024"
    ]
    violations = evaluator.evaluate(text, results)
    assert violations
    assert violations[0].rule_name == "age_date_medical"
    assert violations[0].severity == Severity.PHI


def test_hospital_scenario_no_date_no_violation(evaluator):
    """'A man aged 42 joined a hospital' → not enough QIs to identify.
    Expected: no violation."""
    text = "A man aged 42 joined a hospital"
    results = [
        MockResult("GENDER", 2,  5),    # "man"
        MockResult("AGE",    6, 12),    # "aged 42"
    ]
    violations = evaluator.evaluate(text, results)
    assert not violations, f"unexpected violations: {[(v.rule_name, v.severity) for v in violations]}"


def test_age_date_without_medical_context_does_not_fire(evaluator):
    """'John attended the meeting on Tuesday May 20 2024' — AGE + DATE
    outside medical context must NOT fire age_date_medical."""
    text = "Sarah attended the meeting aged 30 on May 20 2024 at the office"
    results = [
        MockResult("AGE",       27, 34),   # "aged 30"
        MockResult("DATE_TIME", 38, 49),   # "May 20 2024"
    ]
    violations = evaluator.evaluate(text, results)
    # age_date_medical must not fire (no medical context)
    assert not any(v.rule_name == "age_date_medical" for v in violations)


# ------------------------------------------------------------------
# Sweeney canonical (DOB + ZIP + GENDER)
# ------------------------------------------------------------------

def test_sweeney_canonical_high(evaluator):
    """The classic 87% re-identification triad."""
    text = "Female, DOB 1985-03-12, ZIP 94103."
    results = [
        MockResult("GENDER",        0,  6),
        MockResult("DATE_OF_BIRTH", 12, 22),
        MockResult("ZIP_CODE",      28, 33),
    ]
    violations = evaluator.evaluate(text, results)
    assert any(v.rule_name == "sweeney_canonical" for v in violations)
    sweeney = next(v for v in violations if v.rule_name == "sweeney_canonical")
    assert sweeney.severity == Severity.HIGH


def test_sweeney_canonical_in_medical_context_is_phi(evaluator):
    """Same triad near medical context → PHI."""
    text = "Patient: Female, DOB 1985-03-12, ZIP 94103, diagnosis pending."
    results = [
        MockResult("GENDER",        9, 15),
        MockResult("DATE_OF_BIRTH", 21, 31),
        MockResult("ZIP_CODE",      37, 42),
    ]
    violations = evaluator.evaluate(text, results)
    sweeney = next(v for v in violations if v.rule_name == "sweeney_canonical")
    assert sweeney.severity == Severity.PHI


# ------------------------------------------------------------------
# Name + ZIP, Name + Org — new 2-entity rules
# ------------------------------------------------------------------

def test_name_zip_high(evaluator):
    """Full name + 5-digit ZIP → HIGH (usually unique combination)."""
    text = "John Smith lives in 94103."
    results = [
        MockResult("PERSON",   0, 10),    # "John Smith" (full name → granularity 0.9)
        MockResult("ZIP_CODE", 20, 25),   # "94103"
    ]
    violations = evaluator.evaluate(text, results)
    assert any(v.rule_name == "name_zip" for v in violations)


def test_name_zip_first_name_only_does_not_fire(evaluator):
    """First-name only → PERSON granularity 0.4 < required 0.9, doesn't fire."""
    text = "John lives in 94103."
    results = [
        MockResult("PERSON",   0,  4),    # "John" (granularity 0.4)
        MockResult("ZIP_CODE", 14, 19),
    ]
    violations = evaluator.evaluate(text, results)
    assert not any(v.rule_name == "name_zip" for v in violations)


def test_name_org_medium(evaluator):
    text = "John Smith works at Google."
    results = [
        MockResult("PERSON", 0, 10),     # "John Smith"
        MockResult("ORG",   20, 26),     # "Google"
    ]
    violations = evaluator.evaluate(text, results)
    assert any(v.rule_name == "name_org" for v in violations)
    name_org = next(v for v in violations if v.rule_name == "name_org")
    assert name_org.severity == Severity.MEDIUM


# ------------------------------------------------------------------
# Negative case from the original conversation
# ------------------------------------------------------------------

def test_a_man_aged_42_no_violation(evaluator):
    """The user's original gripe: 'A man aged 42 years' should NOT fire PII."""
    text = "A man aged 42 years"
    results = [
        MockResult("GENDER", 2,  5),    # "man"
        MockResult("AGE",    6, 18),    # "aged 42 years"
    ]
    violations = evaluator.evaluate(text, results)
    assert not violations


# ------------------------------------------------------------------
# Ordering invariant — higher-arity rules must come first
# ------------------------------------------------------------------

def test_combo_rule_ordering_is_most_specific_first():
    """If rule A is a strict subset of rule B's required_types, B must come first.
    Otherwise A would consume entities and B could never fire."""
    rules = list(COMBO_RULES)
    for i, rule_a in enumerate(rules):
        for rule_b in rules[i + 1:]:
            if rule_a.required_types.issubset(rule_b.required_types) \
                    and rule_a.required_types != rule_b.required_types:
                pytest.fail(
                    f"{rule_a.name} is a subset of {rule_b.name} but comes "
                    f"before it. Swap their order."
                )