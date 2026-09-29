"""Plain text extractor - always available, no external deps."""

from __future__ import annotations

import os

from interlock.worker.extractors.base import ExtractedContent

_EXTENSIONS = {".txt", ".md", ".rst", ".log"}


class TextExtractor:
    """Extract content from plain text files."""

    def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in _EXTENSIONS:
            return True
        if mime_type and mime_type.startswith("text/plain"):
            return True
        return False

    async def extract(self, file_path: str) -> ExtractedContent:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            text = f.read()

        word_count = len(text.split())
        return ExtractedContent(
            text=text,
            metadata={"source": file_path},
            word_count=word_count,
        )
