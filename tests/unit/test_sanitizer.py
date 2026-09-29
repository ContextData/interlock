"""Unit tests for the RequestSanitizer."""

from __future__ import annotations

import pytest

from interlock.utils.sanitizer import RequestSanitizer


class TestSanitizeSQLIdentifier:
    def test_strips_dangerous_chars(self):
        assert RequestSanitizer.sanitize_sql_identifier("users; DROP TABLE--") == "usersDROPTABLE"

    def test_allows_schema_dot_table(self):
        assert RequestSanitizer.sanitize_sql_identifier("public.users") == "public.users"

    def test_allows_plain_identifier(self):
        assert RequestSanitizer.sanitize_sql_identifier("my_table_1") == "my_table_1"

    def test_rejects_empty_result(self):
        with pytest.raises(ValueError, match="empty after sanitization"):
            RequestSanitizer.sanitize_sql_identifier(";;;")

    def test_strips_consecutive_dots(self):
        result = RequestSanitizer.sanitize_sql_identifier("schema..table")
        assert ".." not in result

    def test_strips_leading_trailing_dots(self):
        result = RequestSanitizer.sanitize_sql_identifier(".table.")
        assert result == "table"


class TestSanitizePath:
    def test_prevents_traversal(self):
        with pytest.raises(ValueError, match="traversal"):
            RequestSanitizer.sanitize_path("/etc/../../../passwd")

    def test_prevents_traversal_dot_dot(self):
        with pytest.raises(ValueError, match="traversal"):
            RequestSanitizer.sanitize_path("../secret")

    def test_allows_normal_path(self):
        result = RequestSanitizer.sanitize_path("/var/data/file.csv")
        assert "file.csv" in result

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="null byte"):
            RequestSanitizer.sanitize_path("/etc/passwd\x00.jpg")


class TestValidateAPIKeyFormat:
    def test_rejects_short_keys(self):
        assert RequestSanitizer.validate_api_key_format("short") is False

    def test_rejects_whitespace(self):
        assert RequestSanitizer.validate_api_key_format("  " + "a" * 32) is False
        assert RequestSanitizer.validate_api_key_format("a" * 16 + " " + "b" * 16) is False

    def test_rejects_all_same_char(self):
        assert RequestSanitizer.validate_api_key_format("a" * 32) is False

    def test_rejects_markup_breakout_characters(self):
        assert RequestSanitizer.validate_api_key_format("a" * 31 + '"') is False
        assert RequestSanitizer.validate_api_key_format("a" * 31 + "<") is False
        assert RequestSanitizer.validate_api_key_format("a" * 31 + "`") is False

    def test_accepts_valid_key(self):
        key = "sk-" + "abcdef1234567890" * 2 + "xyz"
        assert RequestSanitizer.validate_api_key_format(key) is True

    def test_rejects_non_ascii(self):
        key = "a" * 31 + "\xff"
        assert RequestSanitizer.validate_api_key_format(key) is False


class TestMaskSensitiveValue:
    def test_masks_with_default_visible(self):
        result = RequestSanitizer.mask_sensitive_value("sk-abc123xyz789")
        assert result.endswith("z789")
        assert result.startswith("*")
        assert len(result) == len("sk-abc123xyz789")

    def test_masks_short_value(self):
        result = RequestSanitizer.mask_sensitive_value("ab", visible_chars=4)
        assert result == "**"

    def test_masks_empty_value(self):
        result = RequestSanitizer.mask_sensitive_value("")
        assert result == "***"

    def test_custom_visible_chars(self):
        result = RequestSanitizer.mask_sensitive_value("my-secret-value", visible_chars=3)
        assert result.endswith("lue")
        assert result.count("*") == len("my-secret-value") - 3


class TestSanitizeSourceId:
    def test_strips_special_chars(self):
        assert RequestSanitizer.sanitize_source_id("my source!@#") == "mysource"

    def test_allows_hyphens_underscores(self):
        assert RequestSanitizer.sanitize_source_id("my-source_1") == "my-source_1"

    def test_rejects_empty_result(self):
        with pytest.raises(ValueError, match="empty after sanitization"):
            RequestSanitizer.sanitize_source_id("!@#$")
