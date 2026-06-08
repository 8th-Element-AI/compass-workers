"""
Presidio-backed de-identification engine — the single engine for all use cases.

Supports two modes selected at construction time:

Regex-only (instant startup, <15 ms/doc):
    engine = PresidioEngine(ner_model=None)

spaCy model (e.g. en_core_web_sm/lg):
    engine = PresidioEngine(ner_model="en_core_web_lg")

HuggingFace NER model (better recall for PERSON/LOCATION/DATE_TIME):
    engine = PresidioEngine(ner_model="gravitee-io/bert-small-pii-detection")
    engine = PresidioEngine(ner_model="dslim/bert-base-NER")

The model must be downloaded before starting the engine (no downloads during
text processing).

Singleton helper (thread-safe):
    engine = PresidioEngine.get_instance()                       # uses DEIDENTIFIER_NER_MODEL env var
    engine = PresidioEngine.get_instance(ner_model="en_core_web_sm")

Public API:
    engine.process(text, document_id=None)        -> DeidentificationResult
    engine.batch_process(texts, document_ids=...)  -> list[DeidentificationResult]   (sequential)
    engine.analyze(text)                           -> AnalysisResult   (detect only, no anonymize)
    engine.batch_analyze(texts)                    -> list[AnalysisResult]   (sequential)
    engine.analyze_batch(texts, batch_size=4)      -> list[AnalysisResult]   (CONCURRENT — preferred)
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    "CREDIT_CARD", "DATE_TIME", "EMAIL_ADDRESS", "IBAN_CODE", "IP_ADDRESS",
    "LOCATION", "MEDICAL_LICENSE", "PERSON", "PHONE_NUMBER", "URL", "NRP",
    "US_BANK_NUMBER", "US_DRIVER_LICENSE", "US_PASSPORT", "US_SSN",
    "DATE_OF_BIRTH", "MEDICAL_RECORD_NUMBER", "AGE", "ZIP_CODE",
    "MEDICARE_ID", "ORG",
]

_HF_LABEL_MAP: Dict[str, str] = {
    "PER": "PERSON", "PERSON": "PERSON", "NAME": "PERSON",
    "PATIENT": "PERSON", "DOCTOR": "PERSON", "STAFF": "PERSON",
    "HONORIFIC": "PERSON", "TITLE": "PERSON",
    "LOC": "LOCATION", "LOCATION": "LOCATION", "GPE": "LOCATION",
    "CITY": "LOCATION", "STATE": "LOCATION", "COUNTRY": "LOCATION",
    "STREET": "LOCATION", "HOSPITAL": "LOCATION", "HOSP": "LOCATION",
    "COORDINATE": "LOCATION",
    "DATE": "DATE_TIME", "TIME": "DATE_TIME", "DATE_TIME": "DATE_TIME",
    "PHONE": "PHONE_NUMBER", "PHONE_NUMBER": "PHONE_NUMBER", "FAX": "PHONE_NUMBER",
    "EMAIL": "EMAIL_ADDRESS", "EMAIL_ADDRESS": "EMAIL_ADDRESS", "URL": "URL",
    "ID": "MEDICAL_RECORD_NUMBER", "MEDICALRECORD": "MEDICAL_RECORD_NUMBER",
    "SSN": "US_SSN", "US_SSN": "US_SSN", "ZIP": "ZIP_CODE", "AGE": "AGE",
    "US_PASSPORT": "US_PASSPORT", "US_DRIVER_LICENSE": "US_DRIVER_LICENSE",
    "US_BANK_NUMBER": "US_BANK_NUMBER", "CREDIT_CARD": "CREDIT_CARD",
    "IBAN_CODE": "IBAN_CODE", "IP_ADDRESS": "IP_ADDRESS",
    "ORG": "ORG", "ORGANIZATION": "ORG", "PATORG": "ORG", "FINANCIAL": "ORG",
    "OTHERPHI": "NRP", "USERNAME": "NRP", "NRP": "NRP",
    "PROFESSION": "NRP", "MISC": "NRP",
    "MAC_ADDRESS": "NRP", "IMEI": "NRP", "PASSWORD": "NRP",
}


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
        return OperatorConfig("mask", {"masking_char": "*", "chars_to_mask": 500, "from_end": False})
    return OperatorConfig("replace", {"new_value": f"<{entity}>"})


# Default model resolved from env at module import.
_DEFAULT_NER_MODEL = os.getenv("DEIDENTIFIER_NER_MODEL", "gravitee-io/bert-small-pii-detection")
_DEFAULT_NER_MODEL = _DEFAULT_NER_MODEL or None        # empty string -> None (regex-only)


class PresidioEngine:
    """Single configurable de-identification engine.

    Thread-safe singleton: `get_instance()` uses a lock around construction so
    concurrent callers (e.g. a multi-threaded worker manager) never build the
    engine twice. After construction the `ner_model` argument is ignored on
    further calls; a warning is logged if it differs from the loaded model.
    """

    _instance: Optional["PresidioEngine"] = None
    _instance_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------
    @classmethod
    def get_instance(
        cls,
        ner_model: Optional[str] = None,
        policy: Optional[PolicyConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
    ) -> "PresidioEngine":
        # Double-checked locking — fast path avoids the lock once initialised.
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    chosen = ner_model if ner_model is not None else _DEFAULT_NER_MODEL
                    cls._instance = cls(
                        ner_model=chosen, policy=policy, audit_logger=audit_logger,
                    )
                    logger.info(
                        "PresidioEngine singleton created (ner_model=%s).",
                        chosen or "regex-only",
                    )
                    return cls._instance
        # Already exists — warn if caller asked for a different model.
        if ner_model is not None and ner_model != cls._instance.ner_model:
            logger.warning(
                "PresidioEngine.get_instance(ner_model=%r) ignored — singleton "
                "already exists with ner_model=%r.",
                ner_model, cls._instance.ner_model,
            )
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """Drop the cached singleton — useful in tests."""
        with cls._instance_lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(
        self,
        ner_model: Optional[str] = None,
        policy: Optional[PolicyConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
        hf_label_map: Optional[Dict[str, str]] = None,
    ) -> None:
        if not _PRESIDIO_AVAILABLE:
            raise ImportError(
                "Presidio packages are not installed.\n"
                "Run: pip install presidio-analyzer presidio-anonymizer"
            )

        self.ner_model = ner_model        # remembered for the singleton's warning logic
        _use_hf = ner_model is not None and "/" in ner_model
        _spacy_name = "en_core_web_sm" if (ner_model is None or _use_hf) else ner_model

        if _use_hf:
            self._verify_model(ner_model)

        if policy is None:
            policy = PolicyConfig.default()
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
                enable = [p for p in (
                    "transformer", "tok2vec", "tagger", "attribute_ruler",
                    "lemmatizer", "ner",
                ) if p in available]
                lang_nlp.select_pipes(enable=enable)
            logger.info("PresidioEngine NLP backend: spacy (%s)", _spacy_name)

        self._analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
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

    # ==================================================================
    # Public API
    # ==================================================================

    def process(self, text: str, document_id: Optional[str] = None,
                language: str = "en") -> DeidentificationResult:
        import time
        document_id = document_id or str(uuid.uuid4())
        enabled = [e for e in _ALL_ENTITIES if self.policy.is_entity_enabled(e)]

        t0 = time.perf_counter()
        analyzer_results = self._analyzer.analyze(text=text, language=language, entities=enabled)
        analyzer_results = [r for r in analyzer_results if r.score >= self.policy.score_threshold]
        t_id_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        anonymized = self._anonymizer.anonymize(
            text=text, analyzer_results=analyzer_results, operators=self._operators,
        )
        t_deid_ms = (time.perf_counter() - t1) * 1000

        audit_record = AuditRecord(
            document_id=document_id,
            entities_found=len(analyzer_results),
            entities_processed=len(analyzer_results),
        )
        for r in analyzer_results:
            audit_record.add_entry(AuditEntry(
                entity_type=r.entity_type,
                strategy=self.policy.get_entity_strategy(r.entity_type).value,
                start=r.start, end=r.end,
                original_length=r.end - r.start, score=r.score,
            ))
        self.audit_logger.log(audit_record)

        logger.debug("Timings — id:%.1fms deid:%.1fms", t_id_ms, t_deid_ms)

        return DeidentificationResult(
            document_id=document_id, original_text=text,
            deidentified_text=anonymized.text, audit_record=audit_record,
        )

    def batch_process(self, texts: List[str], document_ids: Optional[List[str]] = None,
                      language: str = "en") -> List[DeidentificationResult]:
        """Sequential — kept for backwards-compatibility. Use `analyze_batch`
        for the parallel detection-only path."""
        if document_ids is None:
            document_ids = [None] * len(texts)
        return [self.process(t, did, language) for t, did in zip(texts, document_ids)]

    def analyze(self, text: str, language: str = "en") -> AnalysisResult:
        """Detection only — no text replacement. Faster than process() for
        observability use cases."""
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

    def batch_analyze(self, texts: List[str], language: str = "en") -> List[AnalysisResult]:
        """Sequential `analyze` — kept for callers that don't want concurrency."""
        return [self.analyze(t, language) for t in texts]

    # ------------------------------------------------------------------
    # Concurrent batched analysis (the fast path for observability)
    # ------------------------------------------------------------------
    def analyze_batch(
        self,
        texts: List[str],
        batch_size: int = 4,
        language: str = "en",
    ) -> List[AnalysisResult]:
        """Analyze many texts CONCURRENTLY using a thread pool.

        Args:
            texts: input texts in caller order.
            batch_size: max concurrent in-flight calls (default 4).
            language: NLP language code.

        Returns:
            List of AnalysisResult, same length and order as `texts`. Empty
            strings produce an "empty" result with `has_pii=False`.

        Notes:
            * Identical texts within `texts` are deduped — analyzed once,
              their result is reused for every duplicate position.
            * Concurrency is bounded by Python's GIL on the pure-Python
              regex/recognizer steps, but the underlying spaCy / HuggingFace
              inference releases the GIL during tensor ops, so real-world
              speedup is typically 2-3x for batch_size=4.
            * Thread-safe — each Presidio call is independent and the
              analyzer is read-only after init.
        """
        if not texts:
            return []

        empty = AnalysisResult(has_pii=False, entity_count=0, entities={})

        # Dedup: only run one call per unique non-empty text.
        unique: Dict[str, AnalysisResult] = {}
        ordered_unique: List[str] = []
        for t in texts:
            if not t:
                continue
            if t not in unique:
                unique[t] = empty                                  # placeholder
                ordered_unique.append(t)

        if not ordered_unique:
            return [empty] * len(texts)

        # Concurrent analyze. `executor.map` preserves order.
        with ThreadPoolExecutor(
            max_workers=max(1, batch_size),
            thread_name_prefix="pii",
        ) as pool:
            results = list(pool.map(lambda t: self.analyze(t, language), ordered_unique))
        for t, r in zip(ordered_unique, results):
            unique[t] = r

        # Stitch back to caller order.
        return [unique[t] if t else empty for t in texts]

    # ==================================================================
    # Internal
    # ==================================================================

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