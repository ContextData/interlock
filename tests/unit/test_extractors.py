"""Tests for the content extraction framework."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from interlock.worker.extractors.base import (
    ExtractedContent,
    ExtractionRegistry,
    create_default_registry,
)
from interlock.worker.extractors.csv_extractor import CSVExtractor
from interlock.worker.extractors.html import HTMLExtractor
from interlock.worker.extractors.text import TextExtractor

# ---------------------------------------------------------------------------
# TextExtractor
# ---------------------------------------------------------------------------


class TestTextExtractor:
    def test_can_handle_txt(self):
        ext = TextExtractor()
        assert ext.can_handle("readme.txt") is True
        assert ext.can_handle("notes.md") is True
        assert ext.can_handle("doc.rst") is True
        assert ext.can_handle("app.log") is True

    def test_can_handle_rejects_other(self):
        ext = TextExtractor()
        assert ext.can_handle("data.csv") is False
        assert ext.can_handle("image.png") is False

    def test_can_handle_mime_type(self):
        ext = TextExtractor()
        assert ext.can_handle("noext", mime_type="text/plain") is True
        assert ext.can_handle("noext", mime_type="application/json") is False

    async def test_extract(self, tmp_path):
        f = tmp_path / "sample.txt"
        f.write_text("hello world this is a test")

        ext = TextExtractor()
        result = await ext.extract(str(f))

        assert isinstance(result, ExtractedContent)
        assert result.text == "hello world this is a test"
        assert result.word_count == 6
        assert result.metadata["source"] == str(f)


# ---------------------------------------------------------------------------
# CSVExtractor
# ---------------------------------------------------------------------------


class TestCSVExtractor:
    def test_can_handle(self):
        ext = CSVExtractor()
        assert ext.can_handle("data.csv") is True
        assert ext.can_handle("data.tsv") is True
        assert ext.can_handle("data.txt") is False

    async def test_extract_stdlib_fallback(self, tmp_path):
        """Test CSV extraction with stdlib csv module."""
        f = tmp_path / "data.csv"
        f.write_text("name,age,city\nAlice,30,NYC\nBob,25,LA\n")

        ext = CSVExtractor()
        # Patch pandas as unavailable to force stdlib path
        with patch("interlock.worker.extractors.csv_extractor._HAS_PANDAS", False):
            result = await ext.extract(str(f))

        assert "name" in result.text
        assert "Alice" in result.text
        assert result.metadata["backend"] == "csv_stdlib"
        assert result.metadata["total_rows"] == 2
        assert result.metadata["columns"] == ["name", "age", "city"]

    async def test_extract_with_pandas(self, tmp_path):
        """Test CSV extraction using pandas if available."""
        try:
            import pandas  # noqa: F401
        except ImportError:
            pytest.skip("pandas not installed")

        f = tmp_path / "data.csv"
        f.write_text("x,y\n1,2\n3,4\n")

        ext = CSVExtractor()
        with patch("interlock.worker.extractors.csv_extractor._HAS_PANDAS", True):
            result = await ext.extract(str(f))

        assert "x" in result.text
        assert result.metadata["backend"] == "pandas"


# ---------------------------------------------------------------------------
# HTMLExtractor
# ---------------------------------------------------------------------------


class TestHTMLExtractor:
    def test_can_handle(self):
        ext = HTMLExtractor()
        assert ext.can_handle("page.html") is True
        assert ext.can_handle("page.htm") is True
        assert ext.can_handle("page.txt") is False
        assert ext.can_handle("page", mime_type="text/html") is True

    async def test_extract_with_bs4(self, tmp_path):
        """Test HTML extraction using BeautifulSoup if available."""
        try:
            from bs4 import BeautifulSoup  # noqa: F401
        except ImportError:
            pytest.skip("beautifulsoup4 not installed")

        f = tmp_path / "page.html"
        f.write_text(
            "<html><head><title>Test Page</title></head>"
            "<body><p>Hello world</p><script>var x=1;</script></body></html>"
        )

        ext = HTMLExtractor()
        with patch("interlock.worker.extractors.html._HAS_BS4", True):
            result = await ext.extract(str(f))

        assert "Hello world" in result.text
        assert "var x=1" not in result.text  # script stripped
        assert result.metadata["title"] == "Test Page"
        assert result.metadata["backend"] == "beautifulsoup4"

    async def test_extract_regex_fallback(self, tmp_path):
        """Test HTML extraction with regex when bs4 not available."""
        f = tmp_path / "page.html"
        f.write_text(
            "<html><head><title>My Title</title></head>"
            "<body><p>Content here</p><style>.x{color:red}</style></body></html>"
        )

        ext = HTMLExtractor()
        with patch("interlock.worker.extractors.html._HAS_BS4", False):
            result = await ext.extract(str(f))

        assert "Content here" in result.text
        assert result.metadata["title"] == "My Title"
        assert result.metadata["backend"] == "regex"
        # Style content should be stripped
        assert "color:red" not in result.text


# ---------------------------------------------------------------------------
# DocxExtractor - optional dependency
# ---------------------------------------------------------------------------


class TestDocxExtractor:
    def test_missing_dep_returns_false(self):
        from interlock.worker.extractors.docx import DocxExtractor

        ext = DocxExtractor()
        with patch("interlock.worker.extractors.docx._HAS_DOCX", False):
            assert ext.can_handle("doc.docx") is False
            assert ext.available is False


# ---------------------------------------------------------------------------
# XlsxExtractor - optional dependency
# ---------------------------------------------------------------------------


class TestXlsxExtractor:
    def test_missing_dep_returns_false(self):
        from interlock.worker.extractors.xlsx import XlsxExtractor

        ext = XlsxExtractor()
        with patch("interlock.worker.extractors.xlsx._HAS_OPENPYXL", False):
            assert ext.can_handle("book.xlsx") is False
            assert ext.available is False


# ---------------------------------------------------------------------------
# MistralOCRExtractor
# ---------------------------------------------------------------------------


class TestMistralOCRExtractor:
    def test_no_api_key_returns_false(self):
        from interlock.worker.extractors.mistral_ocr import MistralOCRExtractor

        ext = MistralOCRExtractor()
        with patch.dict(os.environ, {}, clear=True):
            assert ext.can_handle("doc.pdf") is False
            assert ext.available is False

    def test_with_api_key_can_handle(self):
        from interlock.worker.extractors.mistral_ocr import MistralOCRExtractor

        ext = MistralOCRExtractor()
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            assert ext.can_handle("doc.pdf") is True
            assert ext.can_handle("photo.png") is True
            assert ext.can_handle("photo.jpg") is True
            assert ext.can_handle("data.csv") is False
            assert ext.available is True


# ---------------------------------------------------------------------------
# ExtractionRegistry
# ---------------------------------------------------------------------------


class TestExtractionRegistry:
    def test_register_and_get(self):
        registry = ExtractionRegistry()
        text_ext = TextExtractor()
        csv_ext = CSVExtractor()

        registry.register(text_ext)
        registry.register(csv_ext)

        assert registry.get_extractor("readme.txt") is text_ext
        assert registry.get_extractor("data.csv") is csv_ext
        assert registry.get_extractor("unknown.xyz") is None

    def test_first_match_wins(self):
        """If multiple extractors can handle a file, first registered wins."""
        registry = ExtractionRegistry()
        ext1 = TextExtractor()
        ext2 = TextExtractor()

        registry.register(ext1)
        registry.register(ext2)

        assert registry.get_extractor("file.txt") is ext1

    def test_extractors_property(self):
        registry = ExtractionRegistry()
        assert registry.extractors == []
        ext = TextExtractor()
        registry.register(ext)
        assert len(registry.extractors) == 1


# ---------------------------------------------------------------------------
# create_default_registry
# ---------------------------------------------------------------------------


class TestCreateDefaultRegistry:
    def test_includes_always_available(self):
        """Default registry always includes text, csv, and html extractors."""
        registry = create_default_registry()

        # Should find extractors for always-available types
        assert registry.get_extractor("file.txt") is not None
        assert registry.get_extractor("data.csv") is not None
        assert registry.get_extractor("page.html") is not None

    def test_has_at_least_three_extractors(self):
        """At minimum, text + csv + html are always registered."""
        registry = create_default_registry()
        assert len(registry.extractors) >= 3
