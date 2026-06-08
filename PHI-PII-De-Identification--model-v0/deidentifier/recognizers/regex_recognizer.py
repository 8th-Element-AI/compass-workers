from __future__ import annotations

import re
from typing import List, Tuple

from .base import BaseRecognizer, DetectionResult

# (pattern, entity_type, confidence_score)
_PATTERN_DEFS: List[Tuple[str, str, float]] = [
    # Email
    (
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        "EMAIL_ADDRESS",
        0.95,
    ),
    # US phone — (NXX) NXX-XXXX, NXX-NXX-XXXX, NXX.NXX.XXXX, +1 variants
    # Area code must start with 2-9 (NANP); exchange is permissive for de-id coverage
    (
        r"\b(?:\+1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b",
        "PHONE_NUMBER",
        0.85,
    ),
    # SSN  XXX-XX-XXXX  or  XXX XX XXXX
    # Exclude area numbers 000 and 666 (never valid); keep 9XX for de-id coverage
    (r"\b(?!000|666)\d{3}[-\s]\d{2}[-\s]\d{4}\b", "US_SSN", 0.95),
    # Credit card — Visa, MC, Amex, Discover, JCB, Diners
    (
        r"\b(?:4[0-9]{12}(?:[0-9]{3})?|"
        r"5[1-5][0-9]{14}|"
        r"3[47][0-9]{13}|"
        r"3(?:0[0-5]|[68][0-9])[0-9]{11}|"
        r"6(?:011|5[0-9]{2})[0-9]{12}|"
        r"(?:2131|1800|35\d{3})\d{11})\b",
        "CREDIT_CARD",
        0.90,
    ),
    # IPv4
    (
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        "IP_ADDRESS",
        0.92,
    ),
    # URL
    (r"\bhttps?://[^\s/$.?#][^\s]*\b", "URL", 0.90),
    # IBAN — 2 letter country code + 2 check digits + 11-30 alphanumeric
    (r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", "IBAN_CODE", 0.80),
    # US Passport — one letter + 8 digits
    (r"\b[A-Z][0-9]{8}\b", "US_PASSPORT", 0.70),
    # US Driver License (generic, varies by state; common format)
    (r"(?i)(?:driver(?:'s)?\s+license|DL)(?:\s+(?:no\.?|number|#))?[:\s]+([A-Z]?\d{7,9})\b", "US_DRIVER_LICENSE", 0.65),
    # ZIP code — needs context word nearby (checked via lookahead window)
    (r"(?i)(?:zip(?:\s*code)?|postal\s+code)[:\s]+(\d{5}(?:-\d{4})?)", "ZIP_CODE", 0.90),
    (r"\b\d{5}-\d{4}\b", "ZIP_CODE", 0.90),  # ZIP+4 format is unambiguous
    # Medical Record Number
    (
        r"(?i)\b(?:MRN|Medical\s+Record(?:\s+No\.?)?|Patient\s+ID)"
        r"[:\s#\-]*\d{4,10}\b",
        "MEDICAL_RECORD_NUMBER",
        0.92,
    ),
    (r"\bMRN[-\s]?(?:[A-Z]+[-\s])?\d{6,10}\b", "MEDICAL_RECORD_NUMBER", 0.88),
    # Date of birth — must have context keyword
    (
        r"(?i)(?:DOB|Date\s+of\s+Birth|Born|Birth\s+Date|Birthdate)"
        r"[?:\s,]+\d{1,2}[/\-.]\d{1,2}[/\-]\d{2,4}",
        "DATE_OF_BIRTH",
        0.92,
    ),
    (
        r"(?i)(?:DOB|Date\s+of\s+Birth|Born\s+on|Birth\s+Date|Birthdate)"
        r"[?:\s,]+\d{4}-\d{2}-\d{2}",
        "DATE_OF_BIRTH",
        0.92,
    ),
    # Age with context
    (
        r"(?i)(?:age[d]?|years?\s+old)[:\s]+\d{1,3}\b"
        r"|\b\d{1,3}\s*(?:years?|yrs?)[\s\-]+old\b",
        "AGE",
        0.80,
    ),
    # NPI (National Provider Identifier)
    (r"(?i)(?:NPI|National\s+Provider\s+Identifier)[:\s#]+\d{10}\b", "MEDICAL_LICENSE", 0.88),
    # Bank account number with context
    (
        r"(?i)(?:account|acct|bank\s+acct?)(?:\s+(?:no\.?|number|#))?[:\s]+\d{8,17}\b",
        "US_BANK_NUMBER",
        0.80,
    ),
]


class RegexRecognizer(BaseRecognizer):
    def __init__(self) -> None:
        self._compiled = [
            (re.compile(pattern, re.IGNORECASE), entity_type, score)
            for pattern, entity_type, score in _PATTERN_DEFS
        ]

    def analyze(self, text: str) -> List[DetectionResult]:
        results: List[DetectionResult] = []
        for compiled, entity_type, score in self._compiled:
            for match in compiled.finditer(text):
                # Use group(1) when there is a capture group (e.g. ZIP with context)
                matched_text = match.group(1) if match.lastindex else match.group()
                start = match.start(1) if match.lastindex else match.start()
                end = match.end(1) if match.lastindex else match.end()
                results.append(
                    DetectionResult(
                        entity_type=entity_type,
                        start=start,
                        end=end,
                        text=matched_text,
                        score=score,
                        source="regex",
                    )
                )
        return results
