from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .audit import AuditLogger
from .config import PolicyConfig
from .engine import DeidentificationEngine, DeidentificationResult

logger = logging.getLogger(__name__)


@dataclass
class Document:
    id: str
    content: str
    metadata: Dict = field(default_factory=dict)


@dataclass
class ProcessedDocument:
    id: str
    original_content: str
    deidentified_content: str
    metadata: Dict
    entities_found: int
    entities_processed: int
    audit_entries: List[Dict]


class DeidentificationPipeline:
    """
    High-level pipeline that wraps DeidentificationEngine.

    Supports single texts, Document objects, file paths, batch lists,
    and streaming iterators.
    """

    def __init__(
        self,
        policy: Optional[PolicyConfig] = None,
        audit_log_path: Optional[str | Path] = None,
        spacy_model: Optional[str] = "en_core_web_trf",
    ) -> None:
        self._audit_logger = AuditLogger(log_path=audit_log_path)
        self._engine = DeidentificationEngine.get_instance(
            policy=policy,
            audit_logger=self._audit_logger,
            spacy_model=spacy_model,
        )

    # ------------------------------------------------------------------
    # Process methods
    # ------------------------------------------------------------------

    def process_text(
        self, text: str, document_id: Optional[str] = None
    ) -> ProcessedDocument:
        result = self._engine.process(text, document_id)
        return self._to_processed(result, {})

    def process_document(self, document: Document) -> ProcessedDocument:
        result = self._engine.process(document.content, document.id)
        return self._to_processed(result, document.metadata)

    def process_documents(self, documents: List[Document]) -> List[ProcessedDocument]:
        return [self.process_document(doc) for doc in documents]

    def process_texts(self, texts: List[str]) -> List[ProcessedDocument]:
        return [self.process_text(t) for t in texts]

    def process_file(
        self, path: str | Path, encoding: str = "utf-8"
    ) -> ProcessedDocument:
        p = Path(path)
        doc = Document(
            id=p.name,
            content=p.read_text(encoding=encoding),
            metadata={"source_path": str(p)},
        )
        return self.process_document(doc)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def export_audit_log(self, path: str | Path) -> None:
        self._audit_logger.export(path)

    def get_audit_records(self) -> list:
        return [r.to_dict() for r in self._audit_logger.get_records()]

    # ------------------------------------------------------------------

    def _to_processed(
        self, result: DeidentificationResult, metadata: Dict
    ) -> ProcessedDocument:
        return ProcessedDocument(
            id=result.document_id,
            original_content=result.original_text,
            deidentified_content=result.deidentified_text,
            metadata=metadata,
            entities_found=result.audit_record.entities_found,
            entities_processed=result.audit_record.entities_processed,
            audit_entries=[e.to_dict() for e in result.audit_record.entries],
        )
