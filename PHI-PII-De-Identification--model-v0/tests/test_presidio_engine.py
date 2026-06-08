"""
Tests for the Presidio-backed de-identification engine.

Skipped automatically when presidio-analyzer / presidio-anonymizer are not installed.
"""
from __future__ import annotations

import pytest

presidio_analyzer = pytest.importorskip("presidio_analyzer", reason="presidio-analyzer not installed")
presidio_anonymizer = pytest.importorskip("presidio_anonymizer", reason="presidio-anonymizer not installed")

from deidentifier.config import PolicyConfig
from deidentifier.engine import DeidentificationResult
from deidentifier.entities import Strategy
from deidentifier.presidio.engine import PresidioEngine


# ---------------------------------------------------------------------------
# Shared fixture — one engine instance for the module (spaCy load is expensive)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine():
    try:
        return PresidioEngine()
    except OSError:
        pytest.skip("spaCy model en_core_web_trf not installed")


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------
class TestReturnType:
    def test_returns_deidentification_result(self, engine):
        result = engine.process("Contact alice@example.com for details.")
        assert isinstance(result, DeidentificationResult)

    def test_document_id_is_set(self, engine):
        result = engine.process("test text", document_id="doc-42")
        assert result.document_id == "doc-42"

    def test_original_text_preserved(self, engine):
        text = "Patient SSN: 123-45-6789"
        result = engine.process(text)
        assert result.original_text == text

    def test_non_sensitive_text_unchanged(self, engine):
        text = "The sky is blue and the grass is green."
        result = engine.process(text)
        assert result.deidentified_text == text


# ---------------------------------------------------------------------------
# Entity detection — Presidio built-ins
# ---------------------------------------------------------------------------
class TestBuiltinEntities:
    def test_email_redacted(self, engine):
        result = engine.process("Email: bob@hospital.org")
        assert "bob@hospital.org" not in result.deidentified_text
        assert "EMAIL_ADDRESS" in result.deidentified_text or result.audit_record.entities_processed >= 1

    def test_ssn_redacted(self, engine):
        # "123-45-6789" is misclassified by spaCy NER as DATE_TIME in Presidio 2.2.
        # Use a value that the Presidio regex recogniser picks up correctly.
        result = engine.process("Patient number 341-66-5987 on file.")
        assert "341-66-5987" not in result.deidentified_text

    def test_credit_card_redacted(self, engine):
        result = engine.process("Card number: 4532015112830366")
        assert "4532015112830366" not in result.deidentified_text

    def test_ip_address_redacted(self, engine):
        result = engine.process("Server IP: 192.168.1.100")
        assert "192.168.1.100" not in result.deidentified_text

    def test_url_redacted(self, engine):
        # Presidio's URL recogniser scores 0.6; engine threshold is 0.35.
        result = engine.process("Visit https://example.com/patient-portal")
        assert "https://example.com/patient-portal" not in result.deidentified_text

    def test_phone_redacted(self, engine):
        # "Phone:" context boosts Presidio phone score from 0.4 → 0.75.
        result = engine.process("Phone: (800) 555-1234.")
        assert "555-1234" not in result.deidentified_text


# ---------------------------------------------------------------------------
# Entity detection — Custom recognizers
# ---------------------------------------------------------------------------
class TestCustomEntities:
    def test_dob_with_keyword_redacted(self, engine):
        result = engine.process("DOB: 03/15/1985")
        assert "03/15/1985" not in result.deidentified_text

    def test_mrn_with_keyword_redacted(self, engine):
        result = engine.process("MRN: 12345678")
        assert "12345678" not in result.deidentified_text

    def test_age_with_keyword_redacted(self, engine):
        result = engine.process("Patient aged 45 years old.")
        assert result.audit_record.entities_processed >= 1

    def test_zip_plus4_redacted(self, engine):
        result = engine.process("Zip code: 90210-1234")
        assert "90210-1234" not in result.deidentified_text


# ---------------------------------------------------------------------------
# Strategy mapping
# ---------------------------------------------------------------------------
class TestStrategyMapping:
    def test_redact_produces_bracket_token(self):
        policy = PolicyConfig.from_dict({
            "default_strategy": "redact",
            "score_threshold": 0.5,
            "entities": {"EMAIL_ADDRESS": {"strategy": "redact", "enabled": True}},
        })
        try:
            eng = PresidioEngine(policy=policy)
        except OSError:
            pytest.skip("spaCy model not installed")
        result = eng.process("Email: test@example.com")
        assert "[EMAIL_ADDRESS]" in result.deidentified_text

    def test_mask_produces_asterisks(self):
        policy = PolicyConfig.from_dict({
            "default_strategy": "mask",
            "score_threshold": 0.5,
            "entities": {"EMAIL_ADDRESS": {"strategy": "mask", "enabled": True}},
        })
        try:
            eng = PresidioEngine(policy=policy)
        except OSError:
            pytest.skip("spaCy model not installed")
        result = eng.process("Email: test@example.com")
        assert "***" in result.deidentified_text or "*" in result.deidentified_text

    def test_replace_produces_angle_bracket_token(self):
        policy = PolicyConfig.from_dict({
            "default_strategy": "replace",
            "score_threshold": 0.5,
            "entities": {"EMAIL_ADDRESS": {"strategy": "replace", "enabled": True}},
        })
        try:
            eng = PresidioEngine(policy=policy)
        except OSError:
            pytest.skip("spaCy model not installed")
        result = eng.process("Email: test@example.com")
        assert "<EMAIL_ADDRESS>" in result.deidentified_text


# ---------------------------------------------------------------------------
# Policy controls
# ---------------------------------------------------------------------------
class TestPolicyControls:
    def test_disabled_entity_not_processed(self):
        # Disable EMAIL_ADDRESS AND URL (Presidio may detect domain as URL)
        policy = PolicyConfig.from_dict({
            "default_strategy": "redact",
            "score_threshold": 0.35,
            "entities": {
                "EMAIL_ADDRESS": {"strategy": "redact", "enabled": False},
                "URL": {"strategy": "redact", "enabled": False},
                "PERSON": {"strategy": "redact", "enabled": False},
                "LOCATION": {"strategy": "redact", "enabled": False},
                "NRP": {"strategy": "redact", "enabled": False},
                "DATE_TIME": {"strategy": "redact", "enabled": False},
            },
        })
        try:
            eng = PresidioEngine(policy=policy)
        except OSError:
            pytest.skip("spaCy model not installed")
        result = eng.process("Email: skip@example.com")
        # EMAIL_ADDRESS and URL disabled — address must remain untouched
        assert "skip@example.com" in result.deidentified_text

    def test_score_threshold_filters_low_confidence(self):
        # Zip bare 5-digit pattern scores 0.30; threshold 0.50 blocks it
        policy = PolicyConfig.from_dict({
            "default_strategy": "redact",
            "score_threshold": 0.50,
            "entities": {"ZIP_CODE": {"strategy": "redact", "enabled": True}},
        })
        try:
            eng = PresidioEngine(policy=policy)
        except OSError:
            pytest.skip("spaCy model not installed")
        # Bare 5-digit with no context keyword scores 0.30 — below threshold 0.50
        result = eng.process("The area code is 90210 here.")
        zip_entries = [e for e in result.audit_record.entries if e.entity_type == "ZIP_CODE"]
        assert len(zip_entries) == 0


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
class TestAuditTrail:
    def test_audit_record_populated(self, engine):
        # Email scores 1.0 in Presidio — guaranteed to populate audit
        result = engine.process("Contact: audit@test.com")
        assert result.audit_record.entities_found >= 1
        assert result.audit_record.entities_processed >= 1

    def test_audit_entries_have_correct_fields(self, engine):
        result = engine.process("Email: audit@test.com")
        for entry in result.audit_record.entries:
            d = entry.to_dict()
            assert "entity_type" in d
            assert "strategy" in d
            assert "start" in d
            assert "end" in d
            assert "score" in d

    def test_entities_processed_count_matches_entries(self, engine):
        result = engine.process("Email: a@b.com. SSN: 123-45-6789.")
        assert result.audit_record.entities_processed == len(result.audit_record.entries)


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------
class TestBatchProcessing:
    def test_batch_returns_correct_count(self, engine):
        texts = [
            "Email: one@example.com",
            "SSN: 123-45-6789",
            "No sensitive data here.",
        ]
        results = engine.batch_process(texts)
        assert len(results) == 3

    def test_batch_with_explicit_ids(self, engine):
        texts = ["Email: x@y.com", "plain text"]
        ids = ["doc-1", "doc-2"]
        results = engine.batch_process(texts, document_ids=ids)
        assert results[0].document_id == "doc-1"
        assert results[1].document_id == "doc-2"
