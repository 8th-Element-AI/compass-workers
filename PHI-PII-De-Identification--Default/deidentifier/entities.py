from __future__ import annotations

from enum import Enum


class EntityType(str, Enum):
    PERSON = "PERSON"
    EMAIL_ADDRESS = "EMAIL_ADDRESS"
    PHONE_NUMBER = "PHONE_NUMBER"
    US_SSN = "US_SSN"
    CREDIT_CARD = "CREDIT_CARD"
    DATE_TIME = "DATE_TIME"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    LOCATION = "LOCATION"
    US_DRIVER_LICENSE = "US_DRIVER_LICENSE"
    US_PASSPORT = "US_PASSPORT"
    IP_ADDRESS = "IP_ADDRESS"
    IBAN_CODE = "IBAN_CODE"
    MEDICAL_LICENSE = "MEDICAL_LICENSE"
    MEDICAL_RECORD_NUMBER = "MEDICAL_RECORD_NUMBER"
    URL = "URL"
    US_BANK_NUMBER = "US_BANK_NUMBER"
    AGE = "AGE"
    ZIP_CODE = "ZIP_CODE"
    NRP = "NRP"
    MEDICARE_ID = "MEDICARE_ID"
    ORG = "ORG"


class Strategy(str, Enum):
    REDACT = "redact"      # Replace with [ENTITY_TYPE]
    MASK = "mask"          # Replace with *** (same length)
    REPLACE = "replace"    # Replace with synthetic data via Faker
