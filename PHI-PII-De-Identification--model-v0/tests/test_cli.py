"""
Tests for the CLI (deidentifier/cli.py).

Tests the default engine path (regex + spaCy) without requiring
Presidio to be installed. Presidio CLI tests are skipped when the
packages are absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from deidentifier.cli import run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_temp(tmp_path: Path, content: str, name: str = "input.txt") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------

class TestDefaultEngine:
    def test_returns_zero_on_success(self, tmp_path):
        f = _write_temp(tmp_path, "Hello world, no sensitive data.")
        assert run([str(f)]) == 0

    def test_returns_one_on_missing_file(self, tmp_path):
        assert run([str(tmp_path / "nonexistent.txt")]) == 1

    def test_fast_mode_returns_zero(self, tmp_path):
        f = _write_temp(tmp_path, "Contact alice@example.com")
        assert run([str(f), "--fast"]) == 0

    def test_output_file_written(self, tmp_path):
        f = _write_temp(tmp_path, "Email: user@test.com")
        out = tmp_path / "output.txt"
        run([str(f), "--fast", "--output", str(out)])
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "user@test.com" not in content

    def test_non_sensitive_text_preserved(self, tmp_path, capsys):
        text = "The sky is blue."
        f = _write_temp(tmp_path, text)
        run([str(f), "--fast"])
        captured = capsys.readouterr()
        assert "The sky is blue." in captured.out

    def test_ssn_redacted_in_output(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "SSN: 123-45-6789")
        run([str(f), "--fast"])
        captured = capsys.readouterr()
        assert "123-45-6789" not in captured.out

    def test_email_redacted_in_output(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "Contact: info@clinic.org")
        run([str(f), "--fast"])
        captured = capsys.readouterr()
        assert "info@clinic.org" not in captured.out


# ---------------------------------------------------------------------------
# JSON output format
# ---------------------------------------------------------------------------

class TestJsonFormat:
    def test_json_output_is_valid(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "Email: json@example.com")
        run([str(f), "--fast", "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "deidentified_text" in data
        assert "entities_found" in data
        assert "entities_processed" in data
        assert "entries" in data

    def test_json_output_to_file(self, tmp_path):
        f = _write_temp(tmp_path, "SSN: 987-65-4321")
        out = tmp_path / "result.json"
        run([str(f), "--fast", "--format", "json", "--output", str(out)])
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "deidentified_text" in data


# ---------------------------------------------------------------------------
# Strategy override
# ---------------------------------------------------------------------------

class TestStrategyOverride:
    def test_mask_strategy_produces_stars(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "Email: mask@example.com")
        run([str(f), "--fast", "--strategy", "mask"])
        captured = capsys.readouterr()
        assert "*" in captured.out

    def test_redact_strategy_produces_bracket_token(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "SSN: 123-45-6789")
        run([str(f), "--fast", "--strategy", "redact"])
        captured = capsys.readouterr()
        assert "[US_SSN]" in captured.out


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_audit_file_created(self, tmp_path):
        f = _write_temp(tmp_path, "Email: audit@test.com")
        audit = tmp_path / "audit.json"
        run([str(f), "--fast", "--audit", str(audit)])
        assert audit.exists()

    def test_audit_file_is_valid_json(self, tmp_path):
        f = _write_temp(tmp_path, "SSN: 123-45-6789")
        audit = tmp_path / "audit.json"
        run([str(f), "--fast", "--audit", str(audit)])
        data = json.loads(audit.read_text(encoding="utf-8"))
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Score threshold flag
# ---------------------------------------------------------------------------

class TestScoreThreshold:
    def test_strict_threshold_keeps_original(self, tmp_path, capsys):
        # Setting threshold to 1.0 means nothing passes — original text preserved
        f = _write_temp(tmp_path, "SSN: 123-45-6789")
        run([str(f), "--fast", "--score-threshold", "1.0"])
        captured = capsys.readouterr()
        assert "123-45-6789" in captured.out

    def test_permissive_threshold_redacts(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "SSN: 123-45-6789")
        run([str(f), "--fast", "--score-threshold", "0.1"])
        captured = capsys.readouterr()
        assert "123-45-6789" not in captured.out


# ---------------------------------------------------------------------------
# Presidio engine — skipped when not installed
# ---------------------------------------------------------------------------

class TestPresidioEngine:
    @pytest.fixture(autouse=True)
    def _require_presidio(self):
        pytest.importorskip("presidio_analyzer", reason="presidio-analyzer not installed")
        pytest.importorskip("presidio_anonymizer", reason="presidio-anonymizer not installed")

    def test_presidio_engine_returns_zero(self, tmp_path):
        f = _write_temp(tmp_path, "Email: presidio@example.com")
        result = run([str(f), "--engine", "presidio"])
        assert result == 0

    def test_presidio_redacts_email(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "Email: check@example.com")
        run([str(f), "--engine", "presidio"])
        captured = capsys.readouterr()
        assert "check@example.com" not in captured.out

    def test_presidio_json_format(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "SSN: 123-45-6789")
        run([str(f), "--engine", "presidio", "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "deidentified_text" in data
        assert "entries" in data
