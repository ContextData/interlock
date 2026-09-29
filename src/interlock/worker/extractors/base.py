"""Base protocol and registry for content extraction."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ExtractedContent(BaseModel):
    """Result of content extraction from a file."""

    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    page_count: int | None = None
    word_count: int | None = None


class ContentExtractor(Protocol):
    """Protocol that all content extractors must satisfy."""

    def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
        """Return True if this extractor can process the given file."""
        ...

    async def extract(self, file_path: str) -> ExtractedContent:
        """Extract text content from the file."""
        ...


class ExtractionRegistry:
    """Registry of available extractors, selected by file extension/mime type."""

    def __init__(self) -> None:
        self._extractors: list[ContentExtractor] = []

    def register(self, extractor: ContentExtractor) -> None:
        """Add an extractor to the registry."""
        self._extractors.append(extractor)

    def get_extractor(
        self, file_path: str, mime_type: str | None = None
    ) -> ContentExtractor | None:
        """Find the first extractor that can handle the given file."""
        for extractor in self._extractors:
            if extractor.can_handle(file_path, mime_type):
                return extractor
        return None

    @property
    def extractors(self) -> list[ContentExtractor]:
        """Return list of registered extractors."""
        return list(self._extractors)


def create_default_registry() -> ExtractionRegistry:
    """Create registry with all available extractors.

    Each extractor is imported and registered only if it reports
    that its dependencies are satisfied.
    """
    from interlock.worker.extractors.csv_extractor import CSVExtractor
    from interlock.worker.extractors.docx import DocxExtractor
    from interlock.worker.extractors.html import HTMLExtractor
    from interlock.worker.extractors.mistral_ocr import MistralOCRExtractor
    from interlock.worker.extractors.text import TextExtractor
    from interlock.worker.extractors.xlsx import XlsxExtractor

    registry = ExtractionRegistry()

    # Always-available extractors first
    registry.register(TextExtractor())
    registry.register(CSVExtractor())
    registry.register(HTMLExtractor())

    # Optional-dep extractors: register only if deps present
    docx = DocxExtractor()
    if docx.available:
        registry.register(docx)

    xlsx = XlsxExtractor()
    if xlsx.available:
        registry.register(xlsx)

    ocr = MistralOCRExtractor()
    if ocr.available:
        registry.register(ocr)

    logger.info(
        "Extraction registry initialized with %d extractors",
        len(registry.extractors),
    )
    return registry
