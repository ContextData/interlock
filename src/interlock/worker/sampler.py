"""Size-based content sampler for reducing documents before LLM summarization."""

from __future__ import annotations

import re

from pydantic import BaseModel


class SampledContent(BaseModel):
    """Result of sampling a document's content."""

    text: str
    strategy: str  # "full", "strategic", "structure"
    original_word_count: int
    sampled_word_count: int
    sampling_ratio: float


class ContentSampler:
    """Samples document content based on size.

    Small (<2500 words): full content
    Medium (2500-25000 words): 20-30% strategic sample
        (first page, last page, evenly spaced middle sections)
    Large (25000+ words): structure only
        (headings, first sentence of each section)
    """

    SMALL_THRESHOLD = 2500  # words
    MEDIUM_THRESHOLD = 25000  # words

    def sample(self, content: str, page_count: int | None = None) -> SampledContent:
        """Sample content based on size."""
        if not content or not content.strip():
            return SampledContent(
                text="",
                strategy="full",
                original_word_count=0,
                sampled_word_count=0,
                sampling_ratio=1.0,
            )

        word_count = len(content.split())

        if word_count <= self.SMALL_THRESHOLD:
            sampled = self._sample_full(content)
            strategy = "full"
        elif word_count <= self.MEDIUM_THRESHOLD:
            sampled = self._sample_strategic(content)
            strategy = "strategic"
        else:
            sampled = self._sample_structure(content)
            strategy = "structure"

        sampled_word_count = len(sampled.split()) if sampled.strip() else 0
        ratio = sampled_word_count / word_count if word_count > 0 else 1.0

        return SampledContent(
            text=sampled,
            strategy=strategy,
            original_word_count=word_count,
            sampled_word_count=sampled_word_count,
            sampling_ratio=round(ratio, 4),
        )

    # -- sampling strategies ---------------------------------------------------

    def _sample_full(self, content: str) -> str:
        """Return full content for small documents."""
        return content

    def _sample_strategic(self, content: str) -> str:
        """Return a 20-30% strategic sample of medium documents.

        Strategy: split into roughly equal sections, keep the first,
        last, and evenly-spaced middle sections.
        """
        paragraphs = self._split_paragraphs(content)
        if len(paragraphs) <= 5:
            return content

        # Target ~25% of paragraphs
        target = max(3, len(paragraphs) // 4)

        # Always include first and last paragraphs
        selected_indices: list[int] = [0, len(paragraphs) - 1]

        # Fill the rest with evenly spaced paragraphs from the middle
        remaining = target - 2
        if remaining > 0:
            middle_indices = list(range(1, len(paragraphs) - 1))
            step = max(1, len(middle_indices) // (remaining + 1))
            for i in range(remaining):
                idx = min(1 + i * step, len(paragraphs) - 2)
                if idx not in selected_indices:
                    selected_indices.append(idx)

        selected_indices.sort()
        parts = [paragraphs[i] for i in selected_indices]
        return "\n\n".join(parts)

    def _sample_structure(self, content: str) -> str:
        """Return structure-only content for large documents.

        Extracts headings and the first sentence of each section.
        """
        lines = content.splitlines()
        result: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Detect headings: markdown style or ALL CAPS short lines
            if self._is_heading(stripped):
                result.append(stripped)
            # First sentence after a heading (if result ends with a heading)
            elif result and self._is_heading(result[-1]):
                first_sentence = self._first_sentence(stripped)
                if first_sentence:
                    result.append(first_sentence)

        # If we extracted nothing meaningful, fall back to first/last paragraphs
        if not result:
            paragraphs = self._split_paragraphs(content)
            if paragraphs:
                result.append(self._first_sentence(paragraphs[0]) or paragraphs[0][:200])
                if len(paragraphs) > 1:
                    result.append(self._first_sentence(paragraphs[-1]) or paragraphs[-1][:200])

        return "\n\n".join(result)

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _split_paragraphs(content: str) -> list[str]:
        """Split content into paragraphs (double newline separated)."""
        paragraphs = re.split(r"\n\s*\n", content.strip())
        return [p.strip() for p in paragraphs if p.strip()]

    @staticmethod
    def _is_heading(line: str) -> bool:
        """Detect if a line is likely a heading."""
        # Markdown headings
        if line.startswith("#"):
            return True
        # ALL CAPS lines that are short (likely section titles)
        if line.isupper() and len(line.split()) <= 10:
            return True
        # Lines ending with colon that are short
        if line.endswith(":") and len(line.split()) <= 8:
            return True
        return False

    @staticmethod
    def _first_sentence(text: str) -> str:
        """Extract the first sentence from text."""
        match = re.match(r"([^.!?]+[.!?])", text)
        if match:
            return match.group(1).strip()
        # If no sentence boundary found, take the first line
        first_line = text.split("\n")[0].strip()
        return first_line[:300] if first_line else ""
