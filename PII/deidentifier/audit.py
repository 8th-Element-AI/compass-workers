from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    entity_type: str
    strategy: str
    start: int
    end: int
    original_length: int
    score: float
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditRecord:
    document_id: str
    entities_found: int
    entities_processed: int
    entries: List[AuditEntry] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def add_entry(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "entities_found": self.entities_found,
            "entities_processed": self.entities_processed,
            "timestamp": self.timestamp,
            "entries": [e.to_dict() for e in self.entries],
        }


class AuditLogger:
    def __init__(self, log_path: Optional[str | Path] = None):
        self._log_path = Path(log_path) if log_path else None
        self._records: List[AuditRecord] = []

    def log(self, record: AuditRecord) -> None:
        self._records.append(record)
        logger.info(
            "De-identified document=%s entities_processed=%d",
            record.document_id,
            record.entities_processed,
        )
        if self._log_path:
            self._append_to_file(record)

    def _append_to_file(self, record: AuditRecord) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict()) + "\n")

    def get_records(self) -> List[AuditRecord]:
        return list(self._records)

    def export(self, path: str | Path) -> None:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in self._records], f, indent=2)
