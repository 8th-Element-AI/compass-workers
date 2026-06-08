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
            # Keyword immediately adjacent to pure-digit ID
            Pattern(
                name="mrn_keyword_digits",
                regex=(
                    r"(?i)(?:MRN|Medical\s+Record(?:\s+No\.?)?|Patient\s+I(?:D|dentification))"
                    r"[:\s#\-]*\d{4,10}"
                ),
                score=0.92,
            ),
            # Keyword immediately adjacent to alphanumeric ID (e.g. "Patient ID RF-203948")
            Pattern(
                name="mrn_keyword_alphanum",
                regex=(
                    r"(?i)(?:MRN|Medical\s+Record(?:\s+No\.?)?|Patient\s+I(?:D|dentification)|"
                    r"Insurance\s+Policy|Policy)\s*(?:No\.?|Number|#)?"
                    r"[:\s#\-]+[A-Z]{2,4}-[A-Z0-9]{2,8}(?:-[A-Z0-9]{2,8})?"
                ),
                score=0.90,
            ),
            Pattern(
                name="mrn_prefix_attached",
                regex=r"\bMRN[-\s]?\d{6,10}\b",
                score=0.88,
            ),
            # Bare alphanumeric hospital/clinic ID (e.g. RF-203948, MGH-884721)
            # score 0.55 → boosted to 0.90 when a context keyword is nearby
            Pattern(
                name="mrn_alpha_prefix_numeric",
                regex=r"\b[A-Z]{2,4}-\d{4,8}\b",
                score=0.55,
            ),
            # Compound alphanumeric ID (e.g. HLT-9982-AX19 insurance policy)
            # score 0.55 → boosted to 0.90 when a context keyword is nearby
            Pattern(
                name="mrn_compound_alpha",
                regex=r"\b[A-Z]{2,4}-\d{2,6}-[A-Z0-9]{2,6}\b",
                score=0.55,
            ),
            Pattern(
                name="mrn_bare_number",
                regex=r"\b\d{6,10}\b",
                score=0.40,
            ),
        ],
        context=[
            # Single-word entries (effective in substring mode, 5-token window)
            "mrn", "chart", "encounter", "registration", "emr", "ehr",
            "policy",           # catches "insurance policy number was HLT-9982-AX19"
            "identification",   # catches "patient identification number was RF-203948"
            "hospital",         # catches "hospital stay was MGH-884721" (token at -3)
            "record",           # catches "record number", "medical record"
            "medical",          # catches "medical record number"
            # Multi-word phrases (only work if the full phrase fits in one token — kept for
            # future compatibility with whole_word matching mode)
            "medical record", "medical record number", "patient id",
            "patient identification", "patient identification number",
            "identification number", "record number", "chart number",
            "case number", "file number", "visit number", "member id",
            "health record", "record #", "policy number", "insurance policy",
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


def _build_url_recognizer():
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity="URL",
        patterns=[
            # www. prefix — high confidence, no context needed
            Pattern(
                name="url_www_prefix",
                regex=r"\bwww\.[a-zA-Z0-9][a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:[/?#][^\s]*)?\b",
                score=0.85,
            ),
            # Bare domain with common TLDs (e.g. johndoe.com, rajeshweber.net)
            # score 0.50 → boosted to 0.85 when context keyword nearby
            Pattern(
                name="url_bare_common_tld",
                regex=(
                    r"\b[a-zA-Z0-9][a-zA-Z0-9\-]+"
                    r"\.(?:com|net|org|io|co|me|info|edu|gov|mil)\b"
                ),
                score=0.50,
            ),
        ],
        context=[
            "website", "blog", "site", "portfolio", "online", "visit",
            "www", "web", "url", "link", "page", "profile", "personal",
            "my site", "my website", "my blog", "my page",
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
    registry.add_recognizer(_build_url_recognizer())
    registry.add_recognizer(_build_org_recognizer())
