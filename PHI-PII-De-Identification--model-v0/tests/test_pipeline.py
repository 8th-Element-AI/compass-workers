import json
import tempfile
from pathlib import Path

import pytest

from deidentifier import DeidentificationPipeline, PolicyConfig
from deidentifier.pipeline import Document
from .fixtures import CLEAN_TEXT, DISCHARGE_SUMMARY, PATIENT_NOTE


@pytest.fixture(scope="module")
def pipeline():
    return DeidentificationPipeline(spacy_model=None)


class TestProcessText:
    def test_phone_removed(self, pipeline):
        result = pipeline.process_text("Call (555) 123-4567 today.")
        assert "(555) 123-4567" not in result.deidentified_content

    def test_returns_processed_document(self, pipeline):
        result = pipeline.process_text("Hello world")
        assert result.id
        assert result.original_content == "Hello world"
        assert result.deidentified_content is not None


class TestProcessDocument:
    def test_id_preserved(self, pipeline):
        doc = Document(id="rec-001", content=PATIENT_NOTE, metadata={"source": "ehr"})
        result = pipeline.process_document(doc)
        assert result.id == "rec-001"

    def test_metadata_preserved(self, pipeline):
        doc = Document(id="rec-001", content="Test", metadata={"source": "ehr"})
        result = pipeline.process_document(doc)
        assert result.metadata["source"] == "ehr"

    def test_entities_processed_positive(self, pipeline):
        doc = Document(id="x", content=PATIENT_NOTE)
        result = pipeline.process_document(doc)
        assert result.entities_processed > 0


class TestProcessDocuments:
    def test_batch_length(self, pipeline):
        docs = [
            Document(id=f"d-{i}", content=t)
            for i, t in enumerate([PATIENT_NOTE, DISCHARGE_SUMMARY, CLEAN_TEXT])
        ]
        results = pipeline.process_documents(docs)
        assert len(results) == 3

    def test_clean_text_zero_entities(self, pipeline):
        docs = [Document(id="clean", content=CLEAN_TEXT)]
        results = pipeline.process_documents(docs)
        assert results[0].entities_processed == 0


class TestProcessFile:
    def test_file_content_deidentified(self, tmp_path, pipeline):
        f = tmp_path / "note.txt"
        f.write_text(PATIENT_NOTE, encoding="utf-8")
        result = pipeline.process_file(f)
        assert result.entities_processed > 0

    def test_source_path_in_metadata(self, tmp_path, pipeline):
        f = tmp_path / "note.txt"
        f.write_text("Test", encoding="utf-8")
        result = pipeline.process_file(f)
        assert result.metadata["source_path"] == str(f)



class TestAuditTrail:
    def test_audit_entries_have_required_fields(self, pipeline):
        result = pipeline.process_text(PATIENT_NOTE)
        assert len(result.audit_entries) > 0
        for entry in result.audit_entries:
            assert "entity_type" in entry
            assert "strategy" in entry
            assert "start" in entry
            assert "end" in entry
            assert "score" in entry

    def test_export_audit_log_creates_valid_json(self, pipeline):
        pipeline.process_text(PATIENT_NOTE)
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        ) as tmp:
            path = tmp.name
        pipeline.export_audit_log(path)
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        assert isinstance(records, list)
        assert len(records) > 0

    def test_get_audit_records(self, pipeline):
        pipeline.process_text("Contact: info@company.com")
        records = pipeline.get_audit_records()
        assert len(records) > 0
        assert "document_id" in records[0]


class TestProcessTexts:
    def test_list_input(self, pipeline):
        texts = ["SSN: 111-22-3333", "Email: x@y.com"]
        results = pipeline.process_texts(texts)
        assert len(results) == 2
        assert "111-22-3333" not in results[0].deidentified_content
        assert "x@y.com" not in results[1].deidentified_content
