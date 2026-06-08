from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Dict

from faker import Faker

_fake = Faker()

# Used by ReplaceStrategy to generate contextually appropriate fake data per entity type
_ENTITY_FAKER: Dict[str, Callable[[], str]] = {
    "PERSON": lambda: _fake.name(),
    "EMAIL_ADDRESS": lambda: _fake.email(),
    "PHONE_NUMBER": lambda: _fake.phone_number(),
    "US_SSN": lambda: _fake.ssn(),
    "CREDIT_CARD": lambda: _fake.credit_card_number(),
    "DATE_TIME": lambda: _fake.date_time().strftime("%Y-%m-%d"),
    "DATE_OF_BIRTH": lambda: _fake.date_of_birth().strftime("%Y-%m-%d"),
    "LOCATION": lambda: _fake.city(),
    "US_DRIVER_LICENSE": lambda: _fake.bothify("??######"),
    "US_PASSPORT": lambda: _fake.bothify("?#######"),
    "IP_ADDRESS": lambda: _fake.ipv4(),
    "IBAN_CODE": lambda: _fake.iban(),
    "MEDICAL_LICENSE": lambda: _fake.bothify("ML-######"),
    "MEDICAL_RECORD_NUMBER": lambda: f"MRN-{_fake.bothify('########')}",
    "URL": lambda: _fake.url(),
    "US_BANK_NUMBER": lambda: _fake.bban(),
    "AGE": lambda: str(_fake.random_int(min=18, max=90)),
    "ZIP_CODE": lambda: _fake.zipcode(),
    "NRP": lambda: _fake.country(),
    "ORG": lambda: _fake.company(),
    "MEDICARE_ID": lambda: _fake.bothify("#???-??#-??##").upper(),
}


class BaseStrategy(ABC):
    @abstractmethod
    def apply(self, text: str, entity_type: str) -> str:
        pass


class RedactStrategy(BaseStrategy):
    def apply(self, text: str, entity_type: str) -> str:
        return f"[{entity_type}]"


class MaskStrategy(BaseStrategy):
    def apply(self, text: str, entity_type: str) -> str:
        if len(text) <= 4:
            return "*" * len(text)
        visible = min(2, len(text) // 4)
        return text[:visible] + "*" * (len(text) - visible * 2) + text[-visible:]


class ReplaceStrategy(BaseStrategy):
    def apply(self, text: str, entity_type: str) -> str:
        faker_fn = _ENTITY_FAKER.get(entity_type)
        return faker_fn() if faker_fn else f"[{entity_type}]"


_REGISTRY: Dict[str, BaseStrategy] = {
    "redact": RedactStrategy(),
    "mask": MaskStrategy(),
    "replace": ReplaceStrategy(),
}


def get_strategy(name: str) -> BaseStrategy:
    strategy = _REGISTRY.get(name.lower())
    if strategy is None:
        raise ValueError(f"Unknown strategy: {name!r}. Valid options: {list(_REGISTRY)}")
    return strategy
