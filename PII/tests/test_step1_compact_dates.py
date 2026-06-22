"""Step 1 — compact / non-standard date format detection."""
from __future__ import annotations

import pytest

pytest.importorskip("presidio_analyzer")

from deidentifier.presidio.recognizers import _build_compact_date_recognizer


@pytest.fixture(scope="module")
def recognizer():
    return _build_compact_date_recognizer()


def _detect(recognizer, text: str):
    """Run the recognizer in isolation and return the list of matched substrings."""
    results = recognizer.analyze(text, entities=["DATE_TIME"], nlp_artifacts=None)
    return [(text[r.start:r.end], r.score) for r in results]


# ------------------------------------------------------------------
# Formats that today's engine misses but should now be caught
# ------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Admitted on 20thmay2024.",         "20thmay2024"),
    ("Filed 1stJan2025 in the morning.", "1stJan2025"),
    ("3rdfeb24 was the date.",           "3rdfeb24"),
])
def test_compact_with_ordinal(recognizer, text, expected):
    matches = _detect(recognizer, text)
    assert any(m[0].lower() == expected.lower() for m in matches), (
        f"expected to find {expected!r} in {text!r}; got {matches}"
    )


@pytest.mark.parametrize("text, expected", [
    ("Joined on 20may2024 at the clinic.",  "20may2024"),
    ("DOB 5dec99 noted in the chart.",      "5dec99"),
])
def test_compact_no_ordinal(recognizer, text, expected):
    matches = _detect(recognizer, text)
    assert any(m[0].lower() == expected.lower() for m in matches)


@pytest.mark.parametrize("text, expected", [
    ("Visited on 20 May 2024 for a checkup.", "20 May 2024"),
    ("Came in on 20th May 2024.",             "20th May 2024"),
])
def test_spaced_with_optional_ordinal(recognizer, text, expected):
    matches = _detect(recognizer, text)
    assert any(m[0].lower() == expected.lower() for m in matches)


@pytest.mark.parametrize("text, expected", [
    ("Started May 20, 2024 as planned.", "May 20, 2024"),
    ("Booked May 20 2024 morning.",      "May 20 2024"),
    ("Records show May 20th, 2024.",     "May 20th, 2024"),
])
def test_month_first(recognizer, text, expected):
    matches = _detect(recognizer, text)
    assert any(m[0].lower() == expected.lower() for m in matches)


@pytest.mark.parametrize("text, expected", [
    ("Logged 20-May-2024 in the system.", "20-May-2024"),
    ("Entry 20.May.2024 confirmed.",      "20.May.2024"),
    ("Marked 20/May/2024 as final.",      "20/May/2024"),
])
def test_separator_with_month_name(recognizer, text, expected):
    matches = _detect(recognizer, text)
    assert any(m[0].lower() == expected.lower() for m in matches)


@pytest.mark.parametrize("text, expected", [
    ("ISO date 20240520 in metadata.", "20240520"),
    ("Code timestamp 19991231 hit.",   "19991231"),
])
def test_iso_compact(recognizer, text, expected):
    matches = _detect(recognizer, text)
    assert any(m[0] == expected for m in matches)


# ------------------------------------------------------------------
# Negative cases — formats that should NOT be flagged as dates
# ------------------------------------------------------------------

def test_does_not_match_random_8_digit_numbers(recognizer):
    """20240520 is a date; 12345678 is not (fails strict month/day check)."""
    matches = _detect(recognizer, "Reference 12345678 sent.")
    assert not matches, f"unexpected matches: {matches}"


def test_does_not_match_phone_numbers(recognizer):
    matches = _detect(recognizer, "Call 5551234567 for support.")
    # No month-name in the digits → no match
    assert not matches


def test_does_not_match_amounts(recognizer):
    matches = _detect(recognizer, "Total: $1,250.99 today.")
    assert not matches


def test_case_insensitive(recognizer):
    """20MAY2024, 20May2024, 20may2024 all work."""
    for variant in ["20MAY2024", "20May2024", "20may2024"]:
        matches = _detect(recognizer, f"Date {variant} confirmed.")
        assert matches, f"no match for {variant}"