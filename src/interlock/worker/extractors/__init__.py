"""Content extraction framework - pluggable extractors for various file types."""

from interlock.worker.extractors.base import (
    ContentExtractor,
    ExtractedContent,
    ExtractionRegistry,
    create_default_registry,
)

__all__ = [
    "ContentExtractor",
    "ExtractedContent",
    "ExtractionRegistry",
    "create_default_registry",
]
