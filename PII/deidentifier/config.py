from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

import yaml

from .entities import Strategy


@dataclass
class EntityConfig:
    strategy: Strategy = Strategy.REDACT
    enabled: bool = True


@dataclass
class PolicyConfig:
    default_strategy: Strategy = Strategy.REDACT
    score_threshold: float = 0.7
    entities: Dict[str, EntityConfig] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str | Path) -> PolicyConfig:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls._parse(data)

    @classmethod
    def from_dict(cls, data: dict) -> PolicyConfig:
        return cls._parse(data)

    @classmethod
    def _parse(cls, data: dict) -> PolicyConfig:
        cfg = cls(
            default_strategy=Strategy(data.get("default_strategy", "redact")),
            score_threshold=float(data.get("score_threshold", 0.7)),
        )
        for name, entity_data in data.get("entities", {}).items():
            if isinstance(entity_data, dict):
                cfg.entities[name] = EntityConfig(
                    strategy=Strategy(
                        entity_data.get("strategy", cfg.default_strategy.value)
                    ),
                    enabled=entity_data.get("enabled", True),
                )
        return cfg

    @classmethod
    def default(cls) -> PolicyConfig:
        return cls.from_yaml(
            Path(__file__).parent / "policies" / "default.yaml"
        )

    def get_entity_strategy(self, entity_type: str) -> Strategy:
        cfg = self.entities.get(entity_type)
        return cfg.strategy if cfg else self.default_strategy

    def is_entity_enabled(self, entity_type: str) -> bool:
        cfg = self.entities.get(entity_type)
        return cfg.enabled if cfg else True
