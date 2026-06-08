"""
Presidio-backed de-identification engine — the single engine for all use cases.

Supports two modes selected at construction time:

Regex-only (default, instant startup, <15 ms/doc):
    engine = PresidioEngine()
    engine = PresidioEngine(ner_model=None)

HuggingFace NER model (better recall for PERSON/LOCATION/DATE_TIME):
    engine = PresidioEngine(ner_model="gravitee-io/bert-small-pii-detection")
    engine = PresidioEngine(ner_model="dslim/bert-base-NER")
    engine = PresidioEngine(ner_model="obi/deid_roberta_i2b2")

The model must be downloaded before starting the engine (no downloads during
text processing):
    python -c "from transformers import pipeline; \\
               pipeline('ner', model='gravitee-io/bert-small-pii-detection', \\
               aggregation_strategy='first')"

Both modes expose the same interface:
    engine = PresidioEngine.get_instance(ner_model="gravitee-io/bert-small-pii-detection")
    result = engine.process(text, document_id="doc-001")
    results = engine.batch_process([text1, text2])
"""
from __future__ import annotations

import logging
import uuid
from typing import Dict, List, Optional

from ..audit import AuditEntry, AuditLogger, AuditRecord
from ..config import PolicyConfig
from ..result import AnalysisResult, DeidentificationResult
from ..entities import Strategy

logger = logging.getLogger(__name__)

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig
    _PRESIDIO_AVAILABLE = True
except ImportError:
    _PRESIDIO_AVAILABLE = False

# ---------------------------------------------------------------------------
# All entity types detected by this engine
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
# Covers labels from:
#   obi/deid_roberta_i2b2        (i2b2 2014 PHI schema)
#   dslim/bert-base-NER          (CoNLL-2003 schema)
#   gravitee-io/bert-small-pii   (custom PII schema)
# ---------------------------------------------------------------------------
_HF_LABEL_MAP: Dict[str, str] = {
    # Names
    "PER":       "PERSON",
    "PERSON":    "PERSON",
    "NAME":      "PERSON",
    "PATIENT":   "PERSON",
    "DOCTOR":    "PERSON",
    "STAFF":     "PERSON",
    "HONORIFIC": "PERSON",
    "TITLE":     "PERSON",
    # Location
    "LOC":       "LOCATION",
    "LOCATION":  "LOCATION",
    "GPE":       "LOCATION",
    "CITY":      "LOCATION",
    "STATE":     "LOCATION",
    "COUNTRY":   "LOCATION",
    "STREET":    "LOCATION",
    "HOSPITAL":  "LOCATION",
    "HOSP":      "LOCATION",
    "COORDINATE": "LOCATION",
    # Dates / times
    "DATE":      "DATE_TIME",
    "TIME":      "DATE_TIME",
    "DATE_TIME": "DATE_TIME",
    # Contact
    "PHONE":        "PHONE_NUMBER",
    "PHONE_NUMBER": "PHONE_NUMBER",
    "FAX":          "PHONE_NUMBER",
    "EMAIL":        "EMAIL_ADDRESS",
    "EMAIL_ADDRESS": "EMAIL_ADDRESS",
    "URL":          "URL",
    # IDs
    "ID":             "MEDICAL_RECORD_NUMBER",
    "MEDICALRECORD":  "MEDICAL_RECORD_NUMBER",
    "SSN":            "US_SSN",
    "US_SSN":         "US_SSN",
    "ZIP":            "ZIP_CODE",
    "AGE":            "AGE",
    "US_PASSPORT":    "US_PASSPORT",
    "US_DRIVER_LICENSE": "US_DRIVER_LICENSE",
    "US_BANK_NUMBER": "US_BANK_NUMBER",
    "CREDIT_CARD":    "CREDIT_CARD",
    "IBAN_CODE":      "IBAN_CODE",
    "IP_ADDRESS":     "IP_ADDRESS",
    # Org / misc
    "ORG":          "ORG",
    "ORGANIZATION": "ORG",
    "PATORG":       "ORG",
    "FINANCIAL":    "ORG",
    "OTHERPHI":     "NRP",
    "USERNAME":     "NRP",
    "NRP":          "NRP",
    "PROFESSION":   "NRP",
    "MISC":         "NRP",
    "MAC_ADDRESS":  "NRP",
    "IMEI":         "NRP",
    "PASSWORD":     "NRP",
}


# ---------------------------------------------------------------------------
# Long-document NLP engine — patches tokenizer.model_max_length after loading
# so the HuggingFace pipeline uses sliding-window inference instead of raising
# a silent exception on documents longer than 512 tokens.
# ---------------------------------------------------------------------------
class _LongDocNlpEngine:
    """Lazy wrapper — only imported/subclassed when presidio transformers are available."""
    pass


def _build_long_doc_nlp_engine(models, ner_config):
    """Return a TransformersNlpEngine with model_max_length=512 patched in."""
    from presidio_analyzer.nlp_engine import TransformersNlpEngine

    class _Impl(TransformersNlpEngine):
        _BERT_MAX_TOKENS = 512

        def load(self) -> None:
            super().load()
            for nlp in self.nlp.values():
                for _, pipe in nlp.pipeline:
                    if not hasattr(pipe, "hf_pipeline"):
                        continue
                    tok = pipe.hf_pipeline.tokenizer
                    if getattr(tok, "model_max_length", self._BERT_MAX_TOKENS + 1) > self._BERT_MAX_TOKENS:
                        tok.model_max_length = self._BERT_MAX_TOKENS

    return _Impl(models=models, ner_model_configuration=ner_config)


def _make_operator(strategy: Strategy, entity: str) -> "OperatorConfig":
    if strategy == Strategy.REDACT:
        return OperatorConfig("replace", {"new_value": f"[{entity}]"})
    if strategy == Strategy.MASK:
        return OperatorConfig(
            "mask",
            {"masking_char": "*", "chars_to_mask": 500, "from_end": False},
        )
    return OperatorConfig("replace", {"new_value": f"<{entity}>"})


class PresidioEngine:
    """
    Single configurable de-identification engine.

    ner_model selects the NER backend:

      None                                    — regex + Presidio pattern recognizers only.
                                                No NLP model. Instant startup, <15 ms/doc.

      "en_core_web_lg" (or any spaCy name)   — spaCy model handles NER (PERSON, LOCATION …).
                                                Good accuracy, no GPU required.

      "gravitee-io/bert-small-pii-detection"  — HuggingFace transformer NER (default).
      (or any HuggingFace model ID with "/")    Best recall for PII/PHI entities, ~300 ms/doc.
                                                Model must be cached locally before engine starts.

    When a HuggingFace model is used, en_core_web_sm is used internally for
    tokenization only (character offsets); it is not exposed as a parameter.

    Always use get_instance() in application code so the model loads once:
        engine = PresidioEngine.get_instance()
        engine = PresidioEngine.get_instance(ner_model=None)   # regex-only, fast
    """

    _instance: Optional["PresidioEngine"] = None

    @classmethod
    def get_instance(
        cls,
        ner_model: Optional[str] = "gravitee-io/bert-small-pii-detection",
        policy: Optional[PolicyConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
    ) -> "PresidioEngine":
        """Return the shared singleton, creating it on the first call."""
        if cls._instance is None:
            cls._instance = cls(
                ner_model=ner_model,
                policy=policy,
                audit_logger=audit_logger,
            )
            logger.info("PresidioEngine singleton created.")
        return cls._instance

    def __init__(
        self,
        ner_model: Optional[str] = "gravitee-io/bert-small-pii-detection",
        policy: Optional[PolicyConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
        hf_label_map: Optional[Dict[str, str]] = None,
    ) -> None:
        if not _PRESIDIO_AVAILABLE:
            raise ImportError(
                "Presidio packages are not installed.\n"
                "Run: pip install presidio-analyzer presidio-anonymizer"
            )

        # Determine backend: HuggingFace model IDs always contain "/"
        _use_hf = ner_model is not None and "/" in ner_model
        # For the spaCy path: use the model named by ner_model, or sm as the fallback
        _spacy_name = "en_core_web_sm" if (ner_model is None or _use_hf) else ner_model

        if _use_hf:
            self._verify_model(ner_model)

        if policy is None:
            policy = PolicyConfig.default()
            # Presidio built-in recognizer scores are calibrated lower than
            # hand-written regex (e.g. US_SSN medium = 0.5, URL = 0.6).
            # Lower the floor so context-boosted detections pass through.
            policy.score_threshold = 0.35
        self.policy = policy
        self.audit_logger = audit_logger or AuditLogger()

        if _use_hf:
            from presidio_analyzer.nlp_engine import NerModelConfiguration
            ner_config = NerModelConfiguration(
                model_to_presidio_entity_mapping=hf_label_map or _HF_LABEL_MAP,
                aggregation_strategy="first",
                alignment_mode="expand",
                stride=256,
            )
            nlp_engine = _build_long_doc_nlp_engine(
                models=[{
                    "lang_code": "en",
                    "model_name": {"spacy": "en_core_web_sm", "transformers": ner_model},
                }],
                ner_config=ner_config,
            )
            logger.info("PresidioEngine NLP backend: transformers (%s)", ner_model)
        else:
            nlp_configuration = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": _spacy_name}],
                "ner_model_configuration": {
                    "labels_to_ignore": [
                        "CARDINAL", "MONEY", "PERCENT", "ORDINAL", "QUANTITY",
                        "WORK_OF_ART", "EVENT", "FAC", "LANGUAGE", "LAW",
                        "NORP", "PRODUCT",
                    ]
                },
            }
            provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
            nlp_engine = provider.create_engine()
            for lang_nlp in nlp_engine.nlp.values():
                available = lang_nlp.pipe_names
                # Keep tok2vec, tagger, attribute_ruler, lemmatizer (needed for
                # LemmaContextAwareEnhancer score boosting) and ner.
                # Skip parser and senter — not used by any recognizer.
                enable = [
                    p for p in
                    ("transformer", "tok2vec", "tagger", "attribute_ruler", "lemmatizer", "ner")
                    if p in available
                ]
                lang_nlp.select_pipes(enable=enable)
            logger.info("PresidioEngine NLP backend: spacy (%s)", _spacy_name)

        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=["en"],
        )
        from .recognizers import register_all
        register_all(self._analyzer.registry)

        self._anonymizer = AnonymizerEngine()
        self._operators: Dict[str, "OperatorConfig"] = self._build_operators()

        backend = f"transformers:{ner_model}" if _use_hf else f"spacy:{_spacy_name}"
        logger.info(
            "PresidioEngine ready: %d entity types, backend=%s",
            len(_ALL_ENTITIES),
            backend,
        )

    # ------------------------------------------------------------------
    # Public API
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
        t_id_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=analyzer_results,
            operators=self._operators,
        )
        t_deid_ms = (time.perf_counter() - t1) * 1000

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

        logger.info(
            "Timings — identification: %.1fms | de-identification: %.1fms",
            t_id_ms,
            t_deid_ms,
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

    def analyze(self, text: str, language: str = "en") -> AnalysisResult:
        """Detection only — no text replacement.

        Returns whether PII is present, how many entities were found, and a
        per-entity-type count. Skips the anonymization step so it is faster
        than process() for observability use cases.
        """
        enabled = [e for e in _ALL_ENTITIES if self.policy.is_entity_enabled(e)]
        results = self._analyzer.analyze(text=text, language=language, entities=enabled)
        results = [r for r in results if r.score >= self.policy.score_threshold]

        entities: Dict[str, int] = {}
        for r in results:
            entities[r.entity_type] = entities.get(r.entity_type, 0) + 1

        return AnalysisResult(
            has_pii=len(results) > 0,
            entity_count=len(results),
            entities=entities,
        )

    def batch_analyze(
        self,
        texts: List[str],
        language: str = "en",
    ) -> List[AnalysisResult]:
        """Detection only for a list of texts."""
        return [self.analyze(text, language) for text in texts]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_model(ner_model: str) -> None:
        """Raise RuntimeError if a HuggingFace model is not cached locally."""
        try:
            from huggingface_hub import try_to_load_from_cache
            cached = try_to_load_from_cache(ner_model, "config.json")
            if cached is None:
                raise RuntimeError(
                    f"Model '{ner_model}' is not cached locally.\n"
                    f"Download it before starting the engine:\n"
                    f"  python -c \"from transformers import pipeline; "
                    f"pipeline('ner', model='{ner_model}', aggregation_strategy='first')\""
                )
        except ImportError:
            pass

    def _build_operators(self) -> Dict[str, "OperatorConfig"]:
        return {
            entity: _make_operator(self.policy.get_entity_strategy(entity), entity)
            for entity in _ALL_ENTITIES
        }
