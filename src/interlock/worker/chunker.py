"""Text chunking for chunk-level semantic indexing.

Splits documents into overlapping chunks for more precise search retrieval.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import re
from typing import Any

from interlock.models import Chunk


class TextChunker:
    """Splits documents into overlapping chunks for indexing.

    Two strategies:
    - chunk(): word-count based with paragraph awareness and overlap
    - chunk_by_sections(): heading-aware splitting with fallback to word-count
    """

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        min_chunk_size: int = 50,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._min_chunk_size = min_chunk_size

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk(self, text: str, metadata: dict[str, Any] | None = None) -> list[Chunk]:
        """Split text into overlapping chunks.

        Strategy:
        1. Split into paragraphs (double newline)
        2. Accumulate paragraphs into chunks up to chunk_size words
        3. Overlap last chunk_overlap words with next chunk
        4. Attach chunk index and position metadata
        """
        if not text or not text.strip():
            return []

        base_meta = metadata or {}
        words = text.split()
        if not words:
            return []

        # If the entire text fits in one chunk, return it directly
        if len(words) <= self._chunk_size:
            return [
                Chunk(
                    text=text.strip(),
                    index=0,
                    start_word=0,
                    end_word=len(words),
                    metadata=dict(base_meta),
                )
            ]

        paragraphs = _split_paragraphs(text)
        return self._accumulate_chunks(paragraphs, words, base_meta)

    def chunk_by_sections(self, text: str, metadata: dict[str, Any] | None = None) -> list[Chunk]:
        """Split by section headings (markdown # headers, ALL CAPS lines).

        Falls back to word-count chunking within large sections.
        """
        if not text or not text.strip():
            return []

        base_meta = metadata or {}
        sections = _split_sections(text)

        chunks: list[Chunk] = []
        global_word_offset = 0

        for section_text, heading in sections:
            section_words = section_text.split()
            if not section_words:
                continue

            section_meta = dict(base_meta)
            if heading:
                section_meta["section_heading"] = heading

            if len(section_words) <= self._chunk_size:
                if len(section_words) >= self._min_chunk_size:
                    chunks.append(
                        Chunk(
                            text=section_text.strip(),
                            index=len(chunks),
                            start_word=global_word_offset,
                            end_word=global_word_offset + len(section_words),
                            metadata=section_meta,
                        )
                    )
                elif section_words:
                    # Small section - try to merge with previous chunk or keep as-is
                    chunks.append(
                        Chunk(
                            text=section_text.strip(),
                            index=len(chunks),
                            start_word=global_word_offset,
                            end_word=global_word_offset + len(section_words),
                            metadata=section_meta,
                        )
                    )
            else:
                # Large section: fall back to word-count chunking
                sub_chunks = self.chunk(section_text, section_meta)
                for sc in sub_chunks:
                    chunks.append(
                        Chunk(
                            text=sc.text,
                            index=len(chunks),
                            start_word=global_word_offset + sc.start_word,
                            end_word=global_word_offset + sc.end_word,
                            metadata=sc.metadata,
                        )
                    )

            global_word_offset += len(section_words)

        # Re-index
        for i, c in enumerate(chunks):
            c.index = i

        return chunks

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _accumulate_chunks(
        self,
        paragraphs: list[str],
        all_words: list[str],
        base_meta: dict[str, Any],
    ) -> list[Chunk]:
        """Accumulate paragraphs into chunks with overlap."""
        chunks: list[Chunk] = []
        current_words: list[str] = []
        chunk_start_word = 0

        for para in paragraphs:
            para_words = para.split()
            if not para_words:
                continue

            # If adding this paragraph exceeds chunk_size and we already have content
            if current_words and len(current_words) + len(para_words) > self._chunk_size:
                # Emit current chunk
                chunk_text = " ".join(current_words)
                chunks.append(
                    Chunk(
                        text=chunk_text,
                        index=len(chunks),
                        start_word=chunk_start_word,
                        end_word=chunk_start_word + len(current_words),
                        metadata=dict(base_meta),
                    )
                )

                # Start next chunk with overlap from the tail of current
                overlap_words = current_words[-self._chunk_overlap :]
                chunk_start_word = chunk_start_word + len(current_words) - len(overlap_words)
                current_words = list(overlap_words)

            current_words.extend(para_words)

        # Emit final chunk
        if current_words:
            if len(current_words) >= self._min_chunk_size or not chunks:
                chunk_text = " ".join(current_words)
                chunks.append(
                    Chunk(
                        text=chunk_text,
                        index=len(chunks),
                        start_word=chunk_start_word,
                        end_word=chunk_start_word + len(current_words),
                        metadata=dict(base_meta),
                    )
                )
            elif chunks:
                # Too small - merge into previous chunk
                prev = chunks[-1]
                merged_text = prev.text + " " + " ".join(current_words)
                chunks[-1] = Chunk(
                    text=merged_text,
                    index=prev.index,
                    start_word=prev.start_word,
                    end_word=chunk_start_word + len(current_words),
                    metadata=prev.metadata,
                )

        return chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(?:#{1,6}\s+.+|[A-Z][A-Z0-9 ]{2,}[A-Z0-9])$", re.MULTILINE)


def _split_paragraphs(text: str) -> list[str]:
    """Split text on double newlines, preserving non-empty paragraphs."""
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _split_sections(text: str) -> list[tuple[str, str | None]]:
    """Split text by section headings.

    Returns list of (section_text, heading_or_None) tuples.
    Detects markdown headers (# ...) and ALL CAPS lines as headings.
    """
    lines = text.split("\n")
    sections: list[tuple[str, str | None]] = []
    current_lines: list[str] = []
    current_heading: str | None = None

    for line in lines:
        stripped = line.strip()
        if _HEADING_RE.match(stripped):
            # Save previous section
            if current_lines:
                section_text = "\n".join(current_lines).strip()
                if section_text:
                    sections.append((section_text, current_heading))
            current_lines = [line]
            current_heading = stripped.lstrip("#").strip()
        else:
            current_lines.append(line)

    # Final section
    if current_lines:
        section_text = "\n".join(current_lines).strip()
        if section_text:
            sections.append((section_text, current_heading))

    if not sections:
        sections.append((text.strip(), None))

    return sections
