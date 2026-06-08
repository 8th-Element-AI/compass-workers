import pytest

from deidentifier.strategies import (
    MaskStrategy,
    RedactStrategy,
    ReplaceStrategy,
    get_strategy,
)


class TestRedactStrategy:
    def test_replaces_with_entity_label(self):
        result = RedactStrategy().apply("John Smith", "PERSON")
        assert result == "[PERSON]"

    def test_empty_string(self):
        result = RedactStrategy().apply("", "EMAIL_ADDRESS")
        assert result == "[EMAIL_ADDRESS]"


class TestMaskStrategy:
    def test_masks_interior_chars(self):
        result = MaskStrategy().apply("john@example.com", "EMAIL_ADDRESS")
        assert "*" in result
        assert "john@example.com" not in result

    def test_short_input(self):
        result = MaskStrategy().apply("AB", "US_SSN")
        assert all(c == "*" for c in result)

    def test_preserves_length_class(self):
        original = "123-45-6789"
        result = MaskStrategy().apply(original, "US_SSN")
        assert len(result) == len(original)


class TestReplaceStrategy:
    def test_returns_synthetic_email(self):
        result = ReplaceStrategy().apply("real@example.com", "EMAIL_ADDRESS")
        assert result != "real@example.com"
        assert "@" in result

    def test_unknown_entity_falls_back_to_redact(self):
        result = ReplaceStrategy().apply("something", "UNKNOWN_ENTITY")
        assert result == "[UNKNOWN_ENTITY]"

    def test_synthetic_ssn_format(self):
        result = ReplaceStrategy().apply("123-45-6789", "US_SSN")
        assert result != "123-45-6789"


class TestGetStrategy:
    @pytest.mark.parametrize("name", ["redact", "mask", "replace"])
    def test_valid_names(self, name):
        assert get_strategy(name) is not None

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_strategy("nonexistent")
