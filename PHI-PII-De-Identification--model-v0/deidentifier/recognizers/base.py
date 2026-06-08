from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List


@dataclass
class DetectionResult:
    entity_type: str
    start: int
    end: int
    text: str
    score: float
    source: str = "unknown"


class BaseRecognizer(ABC):
    @abstractmethod
    def analyze(self, text: str) -> List[DetectionResult]:
        pass
