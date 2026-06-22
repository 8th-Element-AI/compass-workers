"""Step 4 — engine.analyze() now returns severity-filtered counts."""
from __future__ import annotations

import pytest

pytest.importorskip("presidio_analyzer")
pytest.importorskip("spacy")

# Skip the whole module if the spaCy tokenizer isn't downloaded.
import spacy
try:
    spacy.load("en_core_web_sm")
except OSError:
    pytest.skip("en_core_web_sm not installed", allow_module_level=True)

from deidentifier import PresidioEngine
from deidentifier.policy_evaluator import Severity


@pytest.fixture(scope="module")
def engine():
    """Regex + Presidio patterns + spaCy tokenizer (no HF NER) — fast and offline."""
    PresidioEngine.reset_singleton()
    return PresidioEngine(ner_model=None)


# ------------------------------------------------------------------
# The user's original gripe is now fixed
# ------------------------------------------------------------------

def test_a_man_aged_42_does_not_fire_pii(engine):
    """Quasi-identifier alone (or two weak QIs) must not fire has_pii."""
    result = engine.analyze("A man aged 42 years")
    assert result.has_pii is False
    assert result.entity_count == 0
    assert result.entities == {}
    assert result.severity == Severity.NONE
    # But the raw layer should show what WAS detected
    assert result.raw_entity_count >= 1


def test_hospital_scenario_is_phi(engine):
    """The user's flagship example. age+gender+date+medical → PHI."""
    result = engine.analyze(
        "A man aged 42 years joined a hospital on 20 May 2024"
    )
    assert result.has_pii is True
    assert result.severity == Severity.PHI
    assert result.entity_count >= 3
    # Verify the contributing types are flagged
    assert "AGE" in result.entities
    assert "GENDER" in result.entities
    assert "DATE_TIME" in result.entities


def test_hospital_scenario_with_compact_date(engine):
    """Step 1 + Step 3 together: 20thmay2024 must be caught and contribute."""
    result = engine.analyze(
        "A man aged 42 years joined a hospital on 20thmay2024"
    )
    assert result.has_pii is True
    assert result.severity == Severity.PHI


# ------------------------------------------------------------------
# Direct identifiers still fire correctly
# ------------------------------------------------------------------

def test_email_fires_pii(engine):
    result = engine.analyze("Email me at jane@example.com")
    assert result.has_pii is True
    assert result.severity in (Severity.HIGH, Severity.PHI)
    assert "EMAIL_ADDRESS" in result.entities


def test_ssn_fires_pii(engine):
    result = engine.analyze("SSN 123-45-6789")
    assert result.has_pii is True
    assert result.severity in (Severity.HIGH, Severity.PHI)
    assert "US_SSN" in result.entities


# ------------------------------------------------------------------
# Backwards-compat shape — fields the worker depends on still exist
# ------------------------------------------------------------------

def test_returned_shape_is_backwards_compatible(engine):
    """The Safety worker reads .has_pii, .entity_count, .entities — all
    must remain present with the original types."""
    result = engine.analyze("Email me at jane@example.com")
    assert isinstance(result.has_pii, bool)
    assert isinstance(result.entity_count, int)
    assert isinstance(result.entities, dict)
    # And the new fields exist with sensible defaults
    assert hasattr(result, "severity")
    assert hasattr(result, "violations")
    assert hasattr(result, "raw_entity_count")
    assert hasattr(result, "raw_entities")


# ------------------------------------------------------------------
# Raw detection layer is preserved even when has_pii=False
# ------------------------------------------------------------------

def test_raw_layer_populated_when_filtered_out(engine):
    """If detection happened but didn't reach MEDIUM severity, the raw
    layer should still show what was found — useful for debugging."""
    result = engine.analyze("A man aged 42 years")
    assert result.has_pii is False
    # Raw should show AGE and possibly GENDER were detected
    assert result.raw_entity_count >= 1
    detected_types = set(result.raw_entities.keys())
    assert "AGE" in detected_types or "GENDER" in detected_types


# ------------------------------------------------------------------
# Batch path inherits the same behavior
# ------------------------------------------------------------------

def test_analyze_batch_uses_severity_filter(engine):
    texts = [
        "A man aged 42 years",                                          # no PII
        "A man aged 42 years joined a hospital on 20 May 2024",         # PHI
        "Email me at jane@example.com",                                 # HIGH
    ]
    results = engine.analyze_batch(texts, batch_size=2)
    assert len(results) == 3
    assert results[0].has_pii is False
    assert results[1].has_pii is True and results[1].severity == Severity.PHI
    assert results[2].has_pii is True