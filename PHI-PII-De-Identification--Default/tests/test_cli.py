"""
Tests for the CLI (deidentifier/cli.py).

All tests use regex-only mode (no --ner-model) so they run without GPU or
large model downloads.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from deidentifier.cli import run


def _write_temp(tmp_path: Path, content: str, name: str = "input.txt") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestBasicExecution:
    def test_returns_zero_on_success(self, tmp_path):
        f = _write_temp(tmp_path, "Hello world, no sensitive data.")
        assert run([str(f)]) == 0

    def test_returns_one_on_missing_file(self, tmp_path):
        assert run([str(tmp_path / "nonexistent.txt")]) == 1

    def test_output_file_written(self, tmp_path):
        f = _write_temp(tmp_path, "Email: user@test.com")
        out = tmp_path / "output.txt"
        run([str(f), "--output", str(out)])
        assert out.exists()
        assert "user@test.com" not in out.read_text(encoding="utf-8")

    def test_non_sensitive_text_preserved(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "The sky is blue.")
        run([str(f)])
        assert "The sky is blue." in capsys.readouterr().out

    def test_ssn_redacted(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "SSN: 456-78-9012")
        run([str(f)])
        assert "456-78-9012" not in capsys.readouterr().out

    def test_email_redacted(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "Contact: info@clinic.org")
        run([str(f)])
        assert "info@clinic.org" not in capsys.readouterr().out


class TestJsonFormat:
    def test_json_output_is_valid(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "Email: json@example.com")
        run([str(f), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert "deidentified_text" in data
        assert "entities_found" in data
        assert "entities_processed" in data
        assert "entries" in data

    def test_json_output_to_file(self, tmp_path):
        f = _write_temp(tmp_path, "SSN: 987-65-4321")
        out = tmp_path / "result.json"
        run([str(f), "--format", "json", "--output", str(out)])
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "deidentified_text" in data


class TestStrategyOverride:
    def test_mask_strategy_produces_stars(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "Email: mask@example.com")
        run([str(f), "--strategy", "mask"])
        assert "*" in capsys.readouterr().out

    def test_redact_strategy_produces_bracket_token(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "SSN: 456-78-9012")
        run([str(f), "--strategy", "redact"])
        assert "[US_SSN]" in capsys.readouterr().out


class TestAuditLog:
    def test_audit_file_created(self, tmp_path):
        f = _write_temp(tmp_path, "Email: audit@test.com")
        audit = tmp_path / "audit.json"
        run([str(f), "--audit", str(audit)])
        assert audit.exists()

    def test_audit_file_is_valid_json(self, tmp_path):
        f = _write_temp(tmp_path, "SSN: 456-78-9012")
        audit = tmp_path / "audit.json"
        run([str(f), "--audit", str(audit)])
        data = json.loads(audit.read_text(encoding="utf-8"))
        assert isinstance(data, list)


class TestScoreThreshold:
    def test_strict_threshold_keeps_original(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "SSN: 456-78-9012")
        run([str(f), "--score-threshold", "1.0"])
        assert "456-78-9012" in capsys.readouterr().out

    def test_permissive_threshold_redacts(self, tmp_path, capsys):
        f = _write_temp(tmp_path, "SSN: 456-78-9012")
        run([str(f), "--score-threshold", "0.1"])
        assert "456-78-9012" not in capsys.readouterr().out
