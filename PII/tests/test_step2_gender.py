"""Step 2 — GENDER recognizer."""
from __future__ import annotations

import pytest

pytest.importorskip("presidio_analyzer")

from deidentifier.presidio.recognizers import _build_gender_recognizer


@pytest.fixture(scope="module")
def recognizer():
    return _build_gender_recognizer()


def _detect(recognizer, text: str):
    results = recognizer.analyze(text, entities=["GENDER"], nlp_artifacts=None)
    return [(text[r.start:r.end], r.score) for r in results]


# ------------------------------------------------------------------
# Explicit gender words (singular + plural)
# ------------------------------------------------------------------

@pytest.mark.parametrize("word", [
    "man", "woman", "men", "women",
    "male", "female", "males", "females",
    "boy", "girl", "boys", "girls",
    "gentleman", "lady", "gentlemen", "ladies",
])
def test_gender_nouns_detected(recognizer, word):
    matches = _detect(recognizer, f"The {word} arrived early.")
    assert any(m[0].lower() == word.lower() for m in matches)


# ------------------------------------------------------------------
# Honorifics
# ------------------------------------------------------------------

@pytest.mark.parametrize("honorific", ["Mr.", "Mrs.", "Ms.", "Mx.", "Mister"])
def test_honorific_detected(recognizer, honorific):
    matches = _detect(recognizer, f"{honorific} Smith was seen at 9am.")
    assert any(honorific.rstrip(".") in m[0] for m in matches), (
        f"expected to find {honorific!r}; got {matches}"
    )


def test_honorific_without_period(recognizer):
    """Mr Smith (no period) should still detect."""
    matches = _detect(recognizer, "Mr Jones reviewed it.")
    assert matches


# ------------------------------------------------------------------
# "Gender: Male" / "Sex: F" form (high-confidence clinical pattern)
# ------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Patient details — Gender: Male, Age 42.",
    "Sex: F, DOB 1985-03-12.",
    "gender = female",
])
def test_keyword_value_form_high_confidence(recognizer, text):
    matches = _detect(recognizer, text)
    assert matches, f"no match in {text!r}"
    # The keyword-value pattern has score 0.85; ensure something scored high.
    assert any(m[1] >= 0.85 for m in matches), (
        f"expected a high-confidence match; got {matches}"
    )


# ------------------------------------------------------------------
# Pronouns must NOT be detected (user requirement)
# ------------------------------------------------------------------

@pytest.mark.parametrize("pronoun_text", [
    "He arrived early.",
    "She filed the report.",
    "His appointment was rescheduled.",
    "Her chart is up to date.",
    "We met him yesterday.",
    "The notes are hers.",
])
def test_pronouns_excluded(recognizer, pronoun_text):
    """Pronouns (he/she/his/her/him/hers) must NEVER fire GENDER —
    they appear in nearly every sentence and would explode false positives."""
    matches = _detect(recognizer, pronoun_text)
    assert not matches, f"pronoun unexpectedly matched in {pronoun_text!r}: {matches}"


# ------------------------------------------------------------------
# Case insensitivity
# ------------------------------------------------------------------

def test_case_insensitive(recognizer):
    for variant in ["MALE", "Male", "male", "MaLe"]:
        matches = _detect(recognizer, f"The {variant} patient signed in.")
        assert matches, f"no match for {variant}"