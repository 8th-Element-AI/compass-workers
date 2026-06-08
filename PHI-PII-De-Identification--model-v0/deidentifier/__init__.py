from .audit import AuditLogger
from .config import PolicyConfig
from .engine import DeidentificationEngine, DeidentificationResult
from .entities import EntityType, Strategy
from .pipeline import DeidentificationPipeline, Document, ProcessedDocument
from .presidio.engine import PresidioEngine

__version__ = "1.0.0"

__all__ = [
    # Default engine (regex + spaCy)
    "DeidentificationEngine",
    "DeidentificationResult",
    "DeidentificationPipeline",
    "Document",
    "ProcessedDocument",
    # Presidio engine
    "PresidioEngine",
    # Shared
    "PolicyConfig",
    "EntityType",
    "Strategy",
    "AuditLogger",
]
