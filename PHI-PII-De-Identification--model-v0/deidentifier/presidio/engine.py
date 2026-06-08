"""
Presidio-backed de-identification engine.

Drop-in replacement for DeidentificationEngine: exposes the same
process() / batch_process() interface and returns DeidentificationResult,
so the rest of the codebase (AuditLogger, Pipeline, CLI) is unchanged.

spaCy backend (default):
    pip install presidio-analyzer presidio-anonymizer
    python -m spacy download en_core_web_sm

HuggingFace transformer backend:
    pip install "presidio-analyzer[transformers]" torch
    python -m spacy download en_core_web_sm   # tokenizer only
"""
from __future__ import annotations

import logging
import uuid
from typing import Dict, List, Optional

from ..audit import AuditEntry, AuditLogger, AuditRecord
from ..config import PolicyConfig
from ..engine import DeidentificationResult
from ..entities import Strategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Availability guard — import lazily so the module can be imported even when
# presidio is not installed; the error is raised only when PresidioEngine()
# is instantiated.
# ---------------------------------------------------------------------------
try:
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig

    _PRESIDIO_AVAILABLE = True
except ImportError:
    _PRESIDIO_AVAILABLE = False

# ---------------------------------------------------------------------------
# All entity types understood by this engine
# (16 Presidio built-ins + 5 custom registered via presidio/recognizers.py)
# ---------------------------------------------------------------------------
_ALL_ENTITIES: List[str] = [
    # Presidio built-ins
    "CREDIT_CARD",
    "DATE_TIME",
    "EMAIL_ADDRESS",
    "IBAN_CODE",
    "IP_ADDRESS",
    "LOCATION",
    "MEDICAL_LICENSE",
    "PERSON",
    "PHONE_NUMBER",
    "URL",
    "NRP",
    "US_BANK_NUMBER",
    "US_DRIVER_LICENSE",
    "US_PASSPORT",
    "US_SSN",
    # Custom (registered via presidio/recognizers.py register_all)
    "DATE_OF_BIRTH",
    "MEDICAL_RECORD_NUMBER",
    "AGE",
    "ZIP_CODE",
    "MEDICARE_ID",
    "ORG",
]

# ---------------------------------------------------------------------------
# HuggingFace model label → Presidio entity type
#
# Covers labels from obi/deid_roberta_i2b2 (i2b2 2014 PHI schema) and
# common CoNLL-style NER models (dslim/bert-base-NER, roberta-large-ner).
# ---------------------------------------------------------------------------
_HF_LABEL_MAP: Dict[str, str] = {
    # Names — obi/deid_roberta_i2b2 uses PATIENT/STAFF/DOCTOR instead of PER/NAME
    "PER":       "PERSON",
    "PERSON":    "PERSON",
    "NAME":      "PERSON",
    "PATIENT":   "PERSON",
    "DOCTOR":    "PERSON",
    "STAFF":     "PERSON",   # medical staff (nurses, physicians) in obi/deid schema
    # Location
    "LOC":       "LOCATION",
    "LOCATION":  "LOCATION",
    "GPE":       "LOCATION",
    "CITY":      "LOCATION",
    "STATE":     "LOCATION",
    "COUNTRY":   "LOCATION",
    "STREET":    "LOCATION",
    "HOSPITAL":  "LOCATION",
    "HOSP":      "LOCATION",   # obi/deid_roberta_i2b2 uses HOSP not HOSPITAL
    "OTHERPHI":  "NRP",        # catch-all PHI bucket; closest match for USERNAME
    # Dates / times
    "DATE":      "DATE_TIME",
    "TIME":      "DATE_TIME",
    "DATE_TIME": "DATE_TIME",
    # Contact
    "PHONE":     "PHONE_NUMBER",
    "FAX":       "PHONE_NUMBER",
    "EMAIL":     "EMAIL_ADDRESS",
    "URL":       "URL",
    # IDs
    "ID":             "MEDICAL_RECORD_NUMBER",
    "MEDICALRECORD":  "MEDICAL_RECORD_NUMBER",
    "SSN":            "US_SSN",
    "ZIP":            "ZIP_CODE",
    "AGE":            "AGE",
    # Org / misc
    "ORG":          "ORG",
    "ORGANIZATION": "ORG",
    "PATORG":       "ORG",   # patient-associated org (insurer, clinic) in obi/deid schema
    "USERNAME":     "NRP",
    "PROFESSION":   "NRP",
    "MISC":         "NRP",
}


# ---------------------------------------------------------------------------
# Strategy → Presidio OperatorConfig mapping
#
#   REDACT  → replace with [ENTITY_TYPE]   (bracket notation, clearly redacted)
#   MASK    → mask all chars with *         (Presidio built-in mask operator)
#   REPLACE → replace with <ENTITY_TYPE>    (angle-bracket notation, no Faker needed)
# ---------------------------------------------------------------------------
def _make_operator(strategy: Strategy, entity: str) -> "OperatorConfig":
    if strategy == Strategy.REDACT:
        return OperatorConfig("replace", {"new_value": f"[{entity}]"})
    if strategy == Strategy.MASK:
        return OperatorConfig(
            "mask",
            {"masking_char": "*", "chars_to_mask": 500, "from_end": False},
        )
    # Strategy.REPLACE — built-in token replacement, Faker not required
    return OperatorConfig("replace", {"new_value": f"<{entity}>"})


class PresidioEngine:
    """
    De-identification engine backed by Microsoft Presidio.

    Supports two NLP backends, selected at construction time:

    spaCy (default, faster):
        engine = PresidioEngine(spacy_model="en_core_web_sm")

    HuggingFace transformer (higher accuracy, especially for clinical text):
        engine = PresidioEngine(hf_model="obi/deid_roberta_i2b2")
        engine = PresidioEngine(hf_model="Jean-Baptiste/roberta-large-ner-english")
        engine = PresidioEngine(hf_model="dslim/bert-base-NER")

    Both backends expose the same interface:
        result = engine.process(text, document_id="doc-001")
        results = engine.batch_process([text1, text2])
    """

    _instance: Optional["PresidioEngine"] = None

    @classmethod
    def get_instance(
        cls,
        policy: Optional[PolicyConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
        spacy_model: str = "en_core_web_sm",
        hf_model: Optional[str] = "obi/deid_roberta_i2b2",
    ) -> "PresidioEngine":
        """Return the shared engine instance, creating it on the first call."""
        if cls._instance is None:
            cls._instance = cls(
                policy=policy,
                audit_logger=audit_logger,
                spacy_model=spacy_model,
                hf_model=hf_model,
            )
            logger.info("PresidioEngine singleton created.")
        return cls._instance

    def __init__(
        self,
        policy: Optional[PolicyConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
        spacy_model: str = "en_core_web_sm",
        hf_model: Optional[str] = "obi/deid_roberta_i2b2",
        hf_label_map: Optional[Dict[str, str]] = None,
    ) -> None:
        if not _PRESIDIO_AVAILABLE:
            raise ImportError(
                "Presidio packages are not installed.\n"
                "Run: pip install presidio-analyzer presidio-anonymizer"
            )

        # Presidio confidence scores are calibrated differently from hand-written
        # regex recognizers.  Phone (0.4), URL (0.6), and US_SSN (0.5) all score
        # below the existing engine's default of 0.7, so we use 0.35 as the
        # Presidio-specific floor.  A caller can always pass a custom policy.
        if policy is None:
            policy = PolicyConfig.default()
            policy.score_threshold = 0.35
        self.policy = policy
        self.audit_logger = audit_logger or AuditLogger()

        if hf_model:
            # HuggingFace transformer backend.
            # NerModelConfiguration carries the label mapping from HF model
            # output labels → Presidio entity types. TransformersNlpEngine is
            # created directly (bypassing NlpEngineProvider) so we can pass it.
            # AnalyzerEngine auto-registers TransformersRecognizer when it
            # receives a TransformersNlpEngine — no manual registration needed.
            from presidio_analyzer.nlp_engine import (
                TransformersNlpEngine,
                NerModelConfiguration,
            )
            ner_config = NerModelConfiguration(
                model_to_presidio_entity_mapping=hf_label_map or _HF_LABEL_MAP,
                # "first" uses the label of the first BPE subword token for the
                # entire word. "simple" re-splits BPE groups mid-word, producing
                # fragments like "Kon"/"stantin"/"Becker" instead of "Konstantin".
                aggregation_strategy="first",
                # "expand" snaps HF span boundaries outward to the nearest spaCy
                # token instead of discarding them. "strict" caused most of the
                # "Skipping annotation" warnings for PATIENT/STAFF/LOC/PHONE/EMAIL.
                alignment_mode="expand",
            )
            nlp_engine = TransformersNlpEngine(
                models=[{
                    "lang_code": "en",
                    "model_name": {
                        "spacy": spacy_model,
                        "transformers": hf_model,
                    },
                }],
                ner_model_configuration=ner_config,
            )
            logger.info("PresidioEngine NLP backend: transformers (%s)", hf_model)
        else:
            # spaCy backend via NlpEngineProvider
            nlp_configuration = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": spacy_model}],
            }
            provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
            nlp_engine = provider.create_engine()
            # Restrict pipeline to NER-only components, cutting inference ~55%.
            for lang_nlp in nlp_engine.nlp.values():
                available = lang_nlp.pipe_names
                enable = [p for p in ("transformer", "tok2vec", "ner") if p in available]
                lang_nlp.select_pipes(enable=enable)
                logger.info("spaCy pipeline restricted to: tokenizer + %s", enable)
            logger.info("PresidioEngine NLP backend: spacy (%s)", spacy_model)

        # AnalyzerEngine builds its registry from predefined recognizers.
        # For TransformersNlpEngine it auto-adds TransformersRecognizer.
        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=["en"],
        )

        from .recognizers import register_all
        register_all(self._analyzer.registry)
        self._anonymizer = AnonymizerEngine()

        # Build operators once at startup (one OperatorConfig per entity type)
        self._operators: Dict[str, "OperatorConfig"] = self._build_operators()

        backend = f"transformers:{hf_model}" if hf_model else f"spacy:{spacy_model}"
        logger.info(
            "PresidioEngine ready: %d entity types, backend=%s",
            len(_ALL_ENTITIES),
            backend,
        )

    # ------------------------------------------------------------------
    # Public API — mirrors DeidentificationEngine
    # ------------------------------------------------------------------

    def process(
        self,
        text: str,
        document_id: Optional[str] = None,
        language: str = "en",
    ) -> DeidentificationResult:
        import time
        document_id = document_id or str(uuid.uuid4())

        enabled = [e for e in _ALL_ENTITIES if self.policy.is_entity_enabled(e)]

        # --- Identification ---
        t0 = time.perf_counter()
        analyzer_results = self._analyzer.analyze(
            text=text,
            language=language,
            entities=enabled,
        )
        analyzer_results = [
            r for r in analyzer_results
            if r.score >= self.policy.score_threshold
        ]
        t_identification_ms = (time.perf_counter() - t0) * 1000

        # --- De-identification ---
        t1 = time.perf_counter()
        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=analyzer_results,
            operators=self._operators,
        )
        t_deidentification_ms = (time.perf_counter() - t1) * 1000

        # --- Auditing ---
        t2 = time.perf_counter()
        audit_record = AuditRecord(
            document_id=document_id,
            entities_found=len(analyzer_results),
            entities_processed=len(analyzer_results),
        )
        for r in analyzer_results:
            strategy_name = self.policy.get_entity_strategy(r.entity_type).value
            audit_record.add_entry(
                AuditEntry(
                    entity_type=r.entity_type,
                    strategy=strategy_name,
                    start=r.start,
                    end=r.end,
                    original_length=r.end - r.start,
                    score=r.score,
                )
            )
        self.audit_logger.log(audit_record)
        t_auditing_ms = (time.perf_counter() - t2) * 1000

        logger.info(
            "Timings — identification: %.2fms | de-identification: %.2fms | auditing: %.2fms",
            t_identification_ms,
            t_deidentification_ms,
            t_auditing_ms,
        )

        return DeidentificationResult(
            document_id=document_id,
            original_text=text,
            deidentified_text=anonymized.text,
            audit_record=audit_record,
        )

    def batch_process(
        self,
        texts: List[str],
        document_ids: Optional[List[str]] = None,
        language: str = "en",
    ) -> List[DeidentificationResult]:
        if document_ids is None:
            document_ids = [None] * len(texts)
        return [
            self.process(text, doc_id, language)
            for text, doc_id in zip(texts, document_ids)
        ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_operators(self) -> Dict[str, "OperatorConfig"]:
        operators: Dict[str, "OperatorConfig"] = {}
        for entity in _ALL_ENTITIES:
            strategy = self.policy.get_entity_strategy(entity)
            operators[entity] = _make_operator(strategy, entity)
        return operators
