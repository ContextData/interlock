"""HTML extractor - uses BeautifulSoup if available, falls back to regex."""

from __future__ import annotations

import os
import re

from interlock.worker.extractors.base import ExtractedContent

try:
    from bs4 import BeautifulSoup

    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

_EXTENSIONS = {".html", ".htm"}


class HTMLExtractor:
    """Extract text content from HTML files.

    Uses BeautifulSoup4 for clean extraction when available,
    otherwise strips tags with regex.
    """

    def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in _EXTENSIONS:
            return True
        if mime_type and "html" in mime_type:
            return True
        return False

    async def extract(self, file_path: str) -> ExtractedContent:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            raw = f.read()

        if _HAS_BS4:
            return self._extract_bs4(raw, file_path)
        return self._extract_regex(raw, file_path)

    def _extract_bs4(self, raw: str, file_path: str) -> ExtractedContent:
        soup = BeautifulSoup(raw, "html.parser")

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        # Remove script and style elements
        for tag in soup(["script", "style"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        return ExtractedContent(
            text=text,
            metadata={
                "source": file_path,
                "title": title,
                "backend": "beautifulsoup4",
            },
            word_count=len(text.split()),
        )

    def _extract_regex(self, raw: str, file_path: str) -> ExtractedContent:
        # Extract title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else None

        # Remove script/style blocks
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.IGNORECASE | re.DOTALL)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()

        return ExtractedContent(
            text=text,
            metadata={
                "source": file_path,
                "title": title,
                "backend": "regex",
            },
            word_count=len(text.split()),
        )
