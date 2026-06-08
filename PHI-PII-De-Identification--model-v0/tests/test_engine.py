import pytest

from deidentifier import DeidentificationEngine, PolicyConfig
from .fixtures import (
    CLEAN_TEXT,
    # CRYPTO_TEXT,  # not yet defined in fixtures
    DISCHARGE_SUMMARY,
    MRN_TEXT,
    PATIENT_NOTE,
)


@pytest.fixture(scope="module")
def engine():
    # Disable spaCy to keep tests fast and dependency-free
    return DeidentificationEngine(spacy_model=None)


class TestCleanText:
    def test_non_sensitive_content_preserved(self, engine):
        result = engine.process(CLEAN_TEXT)
        assert "weather" in result.deidentified_text
        assert "sunny" in result.deidentified_text
        assert "successfully" in result.deidentified_text

    def test_zero_entities_processed(self, engine):
        result = engine.process(CLEAN_TEXT)
        assert result.entities_processed == 0


class TestEmailDetection:
    def test_email_removed(self, engine):
        text = "Reach me at user@example.com for details."
        result = engine.process(text)
        assert "user@example.com" not in result.deidentified_text

    def test_text_structure_preserved(self, engine):
        text = "Reach me at user@example.com for details."
        result = engine.process(text)
        assert "Reach me at" in result.deidentified_text
        assert "for details." in result.deidentified_text


class TestPhoneDetection:
    def test_phone_masked(self, engine):
        text = "Call us at (555) 123-4567."
        result = engine.process(text)
        assert "555" not in result.deidentified_text or "*" in result.deidentified_text

    def test_dotted_format(self, engine):
        text = "Call 555.123.4567 now."
        result = engine.process(text)
        assert "555.123.4567" not in result.deidentified_text


class TestSSNDetection:
    def test_ssn_redacted(self, engine):
        text = "SSN: 123-45-6789"
        result = engine.process(text)
        assert "123-45-6789" not in result.deidentified_text

    def test_ssn_with_spaces(self, engine):
        text = "Social Security: 987 65 4321"
        result = engine.process(text)
        assert "987 65 4321" not in result.deidentified_text


class TestCreditCard:
    def test_visa_masked(self, engine):
        text = "Card: 4532015112830366"
        result = engine.process(text)
        assert "4532015112830366" not in result.deidentified_text


class TestMRN:
    def test_mrn_redacted(self, engine):
        result = engine.process(MRN_TEXT)
        assert "987654321" not in result.deidentified_text

    def test_mrn_redact_format(self, engine):
        result = engine.process(MRN_TEXT)
        assert "[MEDICAL_RECORD_NUMBER]" in result.deidentified_text


class TestAuditRecord:
    def test_audit_populated(self, engine):
        result = engine.process(PATIENT_NOTE)
        assert result.audit_record.entities_processed > 0
        assert len(result.audit_record.entries) > 0

    def test_audit_entry_fields(self, engine):
        result = engine.process("Email: test@test.com")
        entry = result.audit_record.entries[0]
        assert entry.entity_type == "EMAIL_ADDRESS"
        assert entry.start >= 0
        assert entry.end > entry.start
        assert entry.score > 0

    def test_document_id_auto_generated(self, engine):
        result = engine.process("Hello world")
        assert result.document_id

    def test_custom_document_id(self, engine):
        result = engine.process("Hello world", document_id="doc-42")
        assert result.document_id == "doc-42"


class TestBatchProcess:
    def test_returns_all_results(self, engine):
        texts = [PATIENT_NOTE, DISCHARGE_SUMMARY, CLEAN_TEXT]
        results = engine.batch_process(texts)
        assert len(results) == 3

    def test_custom_ids(self, engine):
        texts = ["text1", "text2"]
        results = engine.batch_process(texts, document_ids=["id-1", "id-2"])
        assert results[0].document_id == "id-1"
        assert results[1].document_id == "id-2"


class TestCustomPolicy:
    def test_disabled_entity_not_processed(self):
        policy = PolicyConfig.from_dict(
            {
                "default_strategy": "redact",
                "entities": {"EMAIL_ADDRESS": {"strategy": "redact", "enabled": False}},
            }
        )
        eng = DeidentificationEngine(policy=policy, spacy_model=None)
        text = "Email: test@example.com"
        result = eng.process(text)
        assert "test@example.com" in result.deidentified_text

    def test_custom_strategy_applied(self):
        policy = PolicyConfig.from_dict(
            {
                "default_strategy": "redact",
                "entities": {"EMAIL_ADDRESS": {"strategy": "mask", "enabled": True}},
            }
        )
        eng = DeidentificationEngine(policy=policy, spacy_model=None)
        result = eng.process("Email: test@example.com")
        assert "test@example.com" not in result.deidentified_text
        assert "*" in result.deidentified_text
