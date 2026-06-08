"""
Custom Presidio PatternRecognizers for entity types not built into Presidio.

Registers eight entity types:
  - DATE_OF_BIRTH
  - MEDICAL_RECORD_NUMBER
  - AGE
  - ZIP_CODE
  - MEDICAL_LICENSE  (NPI — National Provider Identifier)
  - US_BANK_NUMBER   (routing + account numbers)
  - MEDICARE_ID      (Medicare Beneficiary ID — MBI format)
  - ORG              (organisations detected by spaCy NER ORG label)
"""
from __future__ import annotations


def _build_date_of_birth_recognizer():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="DATE_OF_BIRTH",
        patterns=[
            Pattern(
                name="dob_keyword_slash_dash",
                regex=(
                    r"(?i)(?:DOB|Date\s+of\s+Birth|Born|Birth\s+Date|Birthdate)"
                    r"[:\s]+\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
                ),
                score=0.95,
            ),
            Pattern(
                name="dob_keyword_iso",
                regex=(
                    r"(?i)(?:DOB|Date\s+of\s+Birth|Born|Birth\s+Date|Birthdate)"
                    r"[:\s]+\d{4}-\d{2}-\d{2}"
                ),
                score=0.95,
            ),
            Pattern(
                name="dob_bare_date",
                regex=r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b",
                score=0.40,
            ),
        ],
        context=[
            "dob", "date of birth", "born", "birth date", "birthdate",
            "birthday", "d.o.b", "birth", "born on", "year of birth",
        ],
    )


def _build_mrn_recognizer():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="MEDICAL_RECORD_NUMBER",
        patterns=[
            Pattern(
                name="mrn_keyword_digits",
                regex=(
                    r"(?i)(?:MRN|Medical\s+Record(?:\s+No\.?)?|Patient\s+ID)"
                    r"[:\s#\-]*\d{4,10}"
                ),
                score=0.92,
            ),
            Pattern(
                name="mrn_prefix_attached",
                regex=r"\bMRN[-\s]?\d{6,10}\b",
                score=0.88,
            ),
            Pattern(
                name="mrn_bare_number",
                regex=r"\b\d{6,10}\b",
                score=0.40,
            ),
        ],
        context=[
            "mrn", "medical record", "medical record number", "patient id",
            "record number", "chart", "chart number", "encounter", "case number",
            "file number", "registration", "visit number", "member id",
            "health record", "emr", "ehr", "record #",
        ],
    )


def _build_age_recognizer():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="AGE",
        patterns=[
            Pattern(
                name="age_keyword_before",
                regex=r"(?i)(?:age[d]?|years?\s+old)[:\s]+\d{1,3}",
                score=0.85,
            ),
            Pattern(
                name="age_number_years_old",
                regex=r"\b\d{1,3}\s*(?:years?|yrs?)[\s\-]+old\b",
                score=0.85,
            ),
            Pattern(
                name="age_hyphenated",
                regex=r"\b\d{1,3}-year-old\b",
                score=0.88,
            ),
        ],
        context=[
            "age", "aged", "years old", "yrs", "yr", "years of age",
            "patient age", "pediatric", "geriatric", "neonatal", "adolescent",
            "child", "adult", "elderly", "infant",
        ],
    )


def _build_zip_code_recognizer():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="ZIP_CODE",
        patterns=[
            Pattern(
                name="zip_plus4_format",
                regex=r"\b\d{5}-\d{4}\b",
                score=0.90,
            ),
            Pattern(
                name="zip_bare_5digit",
                regex=r"\b\d{5}\b",
                score=0.40,
            ),
        ],
        context=[
            "zip", "zip code", "zipcode", "postal", "postal code",
            "post code", "mailing code", "city", "state", "address",
        ],
    )


def _build_npi_recognizer():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="MEDICAL_LICENSE",
        patterns=[
            Pattern(
                name="npi_keyword",
                regex=(
                    r"(?i)(?:NPI|National\s+Provider\s+Identifier)"
                    r"(?:\s+#)?[:\s#]+\d{10}\b"
                ),
                score=0.92,
            ),
            # Bare 10-digit fallback — scores below bank (0.41) so ambiguous
            # digit sequences default to US_BANK_NUMBER, not MEDICAL_LICENSE.
            Pattern(
                name="npi_10digit_bare",
                regex=r"\b\d{10}\b",
                score=0.38,
            ),
        ],
        context=[
            "npi", "national provider identifier", "national provider",
            "provider id", "provider identifier", "npi number",
            "prescriber npi", "rendering npi", "billing npi",
            "ordering npi", "referring npi",
        ],
    )


def _build_bank_number_recognizer():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="US_BANK_NUMBER",
        patterns=[
            Pattern(
                name="routing_9digit",
                regex=r"\b\d{9}\b",
                score=0.40,
            ),
            Pattern(
                name="account_8to17digit",
                regex=r"\b\d{8,17}\b",
                score=0.41,
            ),
        ],
        context=[
            "account", "account number", "acct", "routing", "routing number",
            "bank account", "checking", "savings", "aba", "aba number",
            "wire transfer", "direct deposit", "bank routing", "transit",
        ],
    )


def _build_mbi_recognizer():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="MEDICARE_ID",
        patterns=[
            Pattern(
                name="mbi_keyword",
                regex=(
                    r"(?i)(?:Medicare\s+Beneficiary\s+(?:ID|Identifier)|MBI)"
                    r"[:\s#]+[1-9][A-Z][A-Z0-9]{2}-[A-Z0-9]{3}-[A-Z0-9]{4}\b"
                ),
                score=0.95,
            ),
            Pattern(
                name="mbi_bare_dashed",
                regex=r"\b[1-9][A-Z][A-Z0-9]{2}-[A-Z0-9]{3}-[A-Z0-9]{4}\b",
                score=0.80,
            ),
        ],
        context=[
            "medicare", "beneficiary", "mbi", "medicare id", "beneficiary id",
            "medicare beneficiary", "cms", "medicare card",
        ],
    )


def _build_org_recognizer():
    from presidio_analyzer import EntityRecognizer, RecognizerResult

    class SpacyOrgRecognizer(EntityRecognizer):
        def __init__(self):
            super().__init__(supported_entities=["ORG"], name="SpacyOrgRecognizer")

        def load(self):
            pass

        def analyze(self, text, entities, nlp_artifacts=None):  # noqa: ARG002
            results = []
            if not nlp_artifacts or not nlp_artifacts.entities:
                return results
            for ent in nlp_artifacts.entities:
                if ent.label_ not in ("ORG", "ORGANIZATION"):
                    continue
                span_text = text[ent.start_char:ent.end_char]
                # Skip short all-caps acronyms that spaCy mislabels as ORG
                # e.g. "DOB", "SSN", "IP", "MAC", "NLP", "IBAN", "NPI"
                if span_text.isupper() and len(span_text) <= 5:
                    continue
                results.append(
                    RecognizerResult(
                        entity_type="ORG",
                        start=ent.start_char,
                        end=ent.end_char,
                        score=0.85,
                    )
                )
            return results

    return SpacyOrgRecognizer()


def register_all(registry) -> None:
    """Register all custom recognizers into a Presidio RecognizerRegistry."""
    registry.add_recognizer(_build_date_of_birth_recognizer())
    registry.add_recognizer(_build_mrn_recognizer())
    registry.add_recognizer(_build_age_recognizer())
    registry.add_recognizer(_build_zip_code_recognizer())
    registry.add_recognizer(_build_npi_recognizer())
    registry.add_recognizer(_build_bank_number_recognizer())
    registry.add_recognizer(_build_mbi_recognizer())
    registry.add_recognizer(_build_org_recognizer())
