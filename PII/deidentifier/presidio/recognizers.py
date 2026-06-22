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

from presidio_analyzer import Pattern, PatternRecognizer, EntityRecognizer, RecognizerResult



def _build_date_of_birth_recognizer():

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
            # Base score below threshold — only fires when a context keyword boosts it (+0.35).
            # Without context, patterns like INV-2023 (invoice numbers) would be false positives.
            Pattern(
                name="mrn_alpha_prefix_numeric",
                regex=r"\b[A-Z]{2,4}-\d{4,8}\b",
                score=0.20,
            ),
            # Compound alphanumeric ID (e.g. HLT-9982-AX19 insurance policy)
            # Same rationale — invoice numbers like INV-2023-7594 match this pattern.
            Pattern(
                name="mrn_compound_alpha",
                regex=r"\b[A-Z]{2,4}-\d{2,6}-[A-Z0-9]{2,6}\b",
                score=0.20,
            ),
            Pattern(
                name="mrn_bare_number",
                regex=r"\b\d{6,10}\b",
                score=0.20,
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

def _build_compact_date_recognizer():
    """
    Catches date formats that Presidio's built-ins miss:
      - 20thmay2024, 1stJan2025, 3rdfeb24       (ordinal + month jammed against digits)
      - 20may2024, 5dec99                       (no ordinal, no separators)
      - 20 May 2024, 20th May 2024              (spaced with optional ordinal)
      - May 20, 2024, May 20 2024               (month first)
      - 20-May-2024, 20.May.2024, 20/May/2024   (dash/dot/slash + month name)
      - 20240520                                (ISO compact, with strict YYYYMMDD shape)

    Presidio's built-in DateRecognizer handles:
      - 2024-05-20, 05/20/2024, 20/05/2024      (all-numeric, separated)
      - May 20, 2024                            (sometimes — varies by NLP backend)

    Combined coverage: every common written date format users actually type.
    """

    MONTHS = (
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)"
    )

    return PatternRecognizer(
        supported_entity="DATE_TIME",
        patterns=[
            # 20thmay2024, 1stJan2025
            Pattern(
                name="date_compact_ordinal",
                regex=rf"(?i)\b\d{{1,2}}(?:st|nd|rd|th){MONTHS}\d{{2,4}}\b",
                score=0.88,
            ),
            # 20may2024, 5dec99
            Pattern(
                name="date_compact_no_ordinal",
                regex=rf"(?i)\b\d{{1,2}}{MONTHS}\d{{2,4}}\b",
                score=0.85,
            ),
            # 20 May 2024, 20th May 2024
            Pattern(
                name="date_spaced_with_optional_ordinal",
                regex=rf"(?i)\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS}\s+\d{{2,4}}\b",
                score=0.88,
            ),
            # May 20, 2024 / May 20 2024 / May 20th, 2024
            Pattern(
                name="date_month_first",
                regex=rf"(?i)\b{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{2,4}}\b",
                score=0.88,
            ),
            # 20-May-2024, 20.May.2024, 20/May/2024
            Pattern(
                name="date_separator_with_month_name",
                regex=rf"(?i)\b\d{{1,2}}[\-./]{MONTHS}[\-./]\d{{2,4}}\b",
                score=0.88,
            ),
            # ISO compact: 20240520 (strict YYYYMMDD: 1900-2099, valid month, valid day)
            # Strict shape avoids matching arbitrary 8-digit numbers (bank accounts, etc.)
            Pattern(
                name="date_iso_compact",
                regex=r"\b(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\b",
                score=0.70,
            ),
        ],
        context=[
            "date", "on", "admitted", "joined", "born", "dob", "until",
            "since", "from", "scheduled", "appointment", "visit", "discharged",
        ],
    )

def _build_gender_recognizer():
    """
    Catches explicit gender terms and honorifics. NO pronouns
    (he/she/his/her/him/hers) — they appear in nearly every paragraph
    and would explode false positives.

    Gender alone is a weak quasi-identifier (binary or trinary partition
    of population). The score is intentionally just above the default
    threshold so it gets detected, but the value is in combination with
    other QIs — never as a standalone PII alarm.
    """

    return PatternRecognizer(
        supported_entity="GENDER",
        patterns=[
            # Explicit gender nouns (singular + plural)
            Pattern(
                name="gender_noun",
                regex=(
                    r"(?i)\b(?:male|female|males|females|"
                    r"man|woman|men|women|"
                    r"boy|girl|boys|girls|"
                    r"gentleman|lady|gentlemen|ladies)\b"
                ),
                score=0.40,
            ),
            # Honorifics (often adjacent to a name → identifying combination)
            # Mx. is gender-neutral but still a gender marker
            Pattern(
                name="gender_honorific",
                regex=r"(?i)\b(?:Mr|Mrs|Ms|Mx|Mister|Missus)\.?(?=\s|$)",
                score=0.45,
            ),
            # Explicit gender keyword phrases (clinical/form contexts)
            # "Gender: Male", "Sex: F"
            Pattern(
                name="gender_keyword_value",
                regex=r"(?i)\b(?:gender|sex)\s*[:=]\s*(?:m|f|male|female|man|woman|other)\b",
                score=0.85,
            ),
        ],
        context=[
            "gender", "sex", "patient", "identifies as",
            "biological sex", "assigned sex",
        ],
    )

def _build_ssn_recognizer():
    """
    Stronger US_SSN detector than Presidio's built-in.

    Presidio's built-in UsSsnRecognizer scores bare SSN format (XXX-XX-XXXX)
    at only 0.05; even with the "SSN" context boost (+0.35) it lands at 0.4,
    which can fall under stricter score thresholds. This recognizer scores
    the standard formats at high confidence directly, so detection doesn't
    rely on context-boost luck.

    Excludes area numbers 000 and 666 (never valid US SSNs).
    """
    return PatternRecognizer(
        supported_entity="US_SSN",
        patterns=[
            # XXX-XX-XXXX (dashes), XXX XX XXXX (spaces), XXX.XX.XXXX (dots)
            Pattern(
                name="ssn_separated",
                regex=r"\b(?!000|666)\d{3}[-\s.]\d{2}[-\s.]\d{4}\b",
                score=0.85,
            ),
            # 9 consecutive digits — needs context boost to cross threshold,
            # avoiding false positives on bank/phone numbers
            Pattern(
                name="ssn_nine_digits",
                regex=r"\b(?!000000000|666\d{6})\d{9}\b",
                score=0.40,
            ),
        ],
        context=[
            "ssn", "ssn#", "ssn:", "ss#",
            "social security", "social security number",
            "social security #",
        ],
    )


def register_all(registry) -> None:
    """Register all custom recognizers into a Presidio RecognizerRegistry."""
    registry.add_recognizer(_build_date_of_birth_recognizer())
    registry.add_recognizer(_build_compact_date_recognizer())
    registry.add_recognizer(_build_mrn_recognizer())
    registry.add_recognizer(_build_age_recognizer())
    registry.add_recognizer(_build_zip_code_recognizer())
    registry.add_recognizer(_build_npi_recognizer())
    registry.add_recognizer(_build_bank_number_recognizer())
    registry.add_recognizer(_build_mbi_recognizer())
    registry.add_recognizer(_build_url_recognizer())
    registry.add_recognizer(_build_org_recognizer())
    registry.add_recognizer(_build_gender_recognizer())
    registry.add_recognizer(_build_ssn_recognizer())