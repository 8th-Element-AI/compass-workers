from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .audit import AuditRecord


@dataclass
class AnalysisResult:
    """Lightweight result for observability — detection only, no text replacement."""
    has_pii: bool
    entity_count: int
    entities: Dict[str, int]   # entity_type → occurrence count


@dataclass
class DeidentificationResult:
    document_id: str
    original_text: str
    deidentified_text: str
    audit_record: AuditRecord

    @property
    def entities_processed(self) -> int:
        return self.audit_record.entities_processed
