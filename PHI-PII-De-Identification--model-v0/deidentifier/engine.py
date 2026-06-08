from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import List, Optional

from .audit import AuditEntry, AuditLogger, AuditRecord
from .config import PolicyConfig
from .recognizers.base import DetectionResult
from .recognizers.regex_recognizer import RegexRecognizer
from .strategies import get_strategy

logger = logging.getLogger(__name__)


@dataclass
class DeidentificationResult:
    document_id: str
    original_text: str
    deidentified_text: str
    audit_record: AuditRecord

    @property
    def entities_processed(self) -> int:
        return self.audit_record.entities_processed


def _resolve_overlaps(results: List[DetectionResult]) -> List[DetectionResult]:
    """Remove overlapping detections, keeping the highest-scored span."""
    if not results:
        return []
    # Sort by start; on tie prefer the longer and higher-scored span
    ordered = sorted(results, key=lambda r: (r.start, -(r.end - r.start), -r.score))
    resolved: List[DetectionResult] = []
    last_end = -1
    for r in ordered:
        if r.start >= last_end:
            resolved.append(r)
            last_end = r.end
        elif resolved and r.score > resolved[-1].score:
            resolved[-1] = r
            last_end = r.end
    return resolved


class DeidentificationEngine:
    """
    Core de-identification engine.

    Combines a regex-based recognizer (always active) and an optional
    spaCy NER recognizer (requires the spaCy model to be installed).

    Use DeidentificationEngine.get_instance() in application code so the
    spaCy model is loaded only once per process.  Direct instantiation
    via __init__ is still valid for custom policies or isolated test cases.
    """

    _instance: Optional["DeidentificationEngine"] = None

    @classmethod
    def get_instance(
        cls,
        policy: Optional[PolicyConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
        spacy_model: Optional[str] = "en_core_web_trf",
    ) -> "DeidentificationEngine":
        """Return the shared engine instance, creating it on the first call."""
        if cls._instance is None:
            cls._instance = cls(
                policy=policy,
                audit_logger=audit_logger,
                spacy_model=spacy_model,
            )
            logger.info("DeidentificationEngine singleton created.")
        return cls._instance

    def __init__(
        self,
        policy: Optional[PolicyConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
        spacy_model: Optional[str] = "en_core_web_trf",
    ) -> None:
        self.policy = policy or PolicyConfig.default()
        self.audit_logger = audit_logger or AuditLogger()
        self._recognizers = [RegexRecognizer()]

        if spacy_model:
            try:
                from .recognizers.spacy_recognizer import SpacyRecognizer

                self._recognizers.append(SpacyRecognizer(spacy_model))
                logger.info("spaCy recognizer loaded (model=%s).", spacy_model)
            except OSError:
                logger.warning(
                    "spaCy model '%s' not found; running in regex-only mode. "
                    "Install with: python -m spacy download %s",
                    spacy_model,
                    spacy_model,
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
        document_id = document_id or str(uuid.uuid4())

        # 1. Detect entities
        raw: List[DetectionResult] = []
        for recognizer in self._recognizers:
            raw.extend(recognizer.analyze(text))

        # 2. Filter by score threshold and policy-enabled entity types
        active = [
            r
            for r in raw
            if r.score >= self.policy.score_threshold
            and self.policy.is_entity_enabled(r.entity_type)
        ]

        # 3. Deduplicate overlapping spans
        active = _resolve_overlaps(active)

        # 4. Build audit record
        audit_record = AuditRecord(
            document_id=document_id,
            entities_found=len(raw),
            entities_processed=len(active),
        )

        # 5. Apply strategies right-to-left so character positions stay valid
        deidentified = text
        for r in sorted(active, key=lambda x: x.start, reverse=True):
            strategy_name = self.policy.get_entity_strategy(r.entity_type).value
            replacement = get_strategy(strategy_name).apply(r.text, r.entity_type)
            deidentified = deidentified[: r.start] + replacement + deidentified[r.end :]
            audit_record.add_entry(
                AuditEntry(
                    entity_type=r.entity_type,
                    strategy=strategy_name,
                    start=r.start,
                    end=r.end,
                    original_length=len(r.text),
                    score=r.score,
                )
            )

        # 6. Log audit record
        self.audit_logger.log(audit_record)

        return DeidentificationResult(
            document_id=document_id,
            original_text=text,
            deidentified_text=deidentified,
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
