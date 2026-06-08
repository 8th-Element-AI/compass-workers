from __future__ import annotations

import logging
from typing import Any, Dict, List

from .base import BaseRecognizer, DetectionResult

logger = logging.getLogger(__name__)

# Module-level cache: model name → loaded spaCy Language object.
# Ensures spacy.load() is called exactly once per model name per process,
# regardless of how many SpacyRecognizer or DeidentificationEngine instances
# are created.
_MODEL_CACHE: Dict[str, Any] = {}

# Components not needed for NER-only de-identification.
# Using exclude= (not disable=) so weights are never loaded into memory.
_EXCLUDED_COMPONENTS = ["parser", "tagger", "morphologizer", "lemmatizer"]

# Maps spaCy NER label → our entity type; unmapped labels are ignored.
# LOC (rivers, mountains) and FAC (buildings, airports) are deliberately excluded:
# they produce far more FPs than TPs because spaCy frequently mislabels
# common nouns ("Indian market", "Apple campus") with these labels.
# GPE (cities, states, countries) is precise enough to keep.
_LABEL_MAP = {
    "PERSON": "PERSON",
    "GPE": "LOCATION",     # geopolitical entity (city, state, country)
    "DATE": "DATE_TIME",
    "TIME": "DATE_TIME",
    "ORG": "NRP",          # organization names treated as NRP for healthcare context
}


def _is_label_abbreviation(text: str) -> bool:
    """Skip all-uppercase short tokens — these are field labels (SSN:, DOB:, MRN:), not real entities."""
    stripped = text.strip().rstrip(":")
    return stripped.isupper() and len(stripped) <= 5


class SpacyRecognizer(BaseRecognizer):
    def __init__(self, model: str = "en_core_web_trf") -> None:
        import spacy  # deferred so import error only occurs when this class is used

        if model not in _MODEL_CACHE:
            try:
                _MODEL_CACHE[model] = spacy.load(model, exclude=_EXCLUDED_COMPONENTS)
                logger.info(
                    "spaCy model '%s' loaded (excluded: %s).",
                    model,
                    ", ".join(_EXCLUDED_COMPONENTS),
                )
            except OSError:
                logger.error(
                    "spaCy model '%s' not found. Install with: python -m spacy download %s",
                    model,
                    model,
                )
                raise
        else:
            logger.debug("spaCy model '%s' reused from cache.", model)

        self._nlp = _MODEL_CACHE[model]

    def analyze(self, text: str) -> List[DetectionResult]:
        doc = self._nlp(text)
        results: List[DetectionResult] = []
        for ent in doc.ents:
            entity_type = _LABEL_MAP.get(ent.label_)
            if entity_type is None:
                continue
            if _is_label_abbreviation(ent.text):
                continue
            results.append(
                DetectionResult(
                    entity_type=entity_type,
                    start=ent.start_char,
                    end=ent.end_char,
                    text=ent.text,
                    score=0.85,
                    source="spacy",
                )
            )
        return results
