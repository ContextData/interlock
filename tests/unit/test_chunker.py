"""Tests for TextChunker - chunk-level document splitting."""

from __future__ import annotations

from interlock.models import Chunk
from interlock.worker.chunker import TextChunker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    return len(text.split())


def _make_long_text(n_words: int, paragraph_size: int = 80) -> str:
    """Generate text with multiple paragraphs totaling roughly n_words."""
    paragraphs = []
    words_remaining = n_words
    word_num = 0
    while words_remaining > 0:
        count = min(paragraph_size, words_remaining)
        words = [f"word{word_num + i}" for i in range(count)]
        paragraphs.append(" ".join(words))
        words_remaining -= count
        word_num += count
    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# Tests: chunk()
# ---------------------------------------------------------------------------


class TestChunkBasic:
    def test_empty_text(self):
        chunker = TextChunker()
        assert chunker.chunk("") == []
        assert chunker.chunk("   ") == []

    def test_short_text_single_chunk(self):
        chunker = TextChunker(chunk_size=512)
        text = "This is a short document with just a few words."
        chunks = chunker.chunk(text)
        assert len(chunks) == 1
        assert chunks[0].index == 0
        assert chunks[0].start_word == 0
        assert chunks[0].text == text

    def test_long_text_multiple_chunks(self):
        chunker = TextChunker(chunk_size=100, chunk_overlap=10, min_chunk_size=20)
        text = _make_long_text(500)
        chunks = chunker.chunk(text)
        assert len(chunks) > 1
        # All chunks should be Chunk instances
        for c in chunks:
            assert isinstance(c, Chunk)
        # Indices should be sequential
        for i, c in enumerate(chunks):
            assert c.index == i

    def test_overlap_between_chunks(self):
        overlap = 10
        chunker = TextChunker(chunk_size=100, chunk_overlap=overlap, min_chunk_size=5)
        text = _make_long_text(300)
        chunks = chunker.chunk(text)
        assert len(chunks) >= 2

        # Check that consecutive chunks share overlapping words
        for i in range(len(chunks) - 1):
            curr_words = chunks[i].text.split()
            next_words = chunks[i + 1].text.split()
            # The tail of the current chunk should appear at the start of the next
            tail = curr_words[-overlap:]
            head = next_words[:overlap]
            assert tail == head, f"Chunks {i} and {i+1} should share {overlap} overlap words"

    def test_min_chunk_size_filter(self):
        """Chunks smaller than min_chunk_size get merged into previous."""
        chunker = TextChunker(chunk_size=100, chunk_overlap=0, min_chunk_size=30)
        # Create text where the remainder is < 30 words
        text = _make_long_text(120, paragraph_size=110)
        chunks = chunker.chunk(text)
        # Should merge the small tail into the previous chunk
        for c in chunks:
            assert _word_count(c.text) >= 30 or len(chunks) == 1

    def test_metadata_propagation(self):
        chunker = TextChunker(chunk_size=100, chunk_overlap=10, min_chunk_size=5)
        meta = {"source": "test", "doc_id": 42}
        text = _make_long_text(300)
        chunks = chunker.chunk(text, metadata=meta)
        for c in chunks:
            assert c.metadata["source"] == "test"
            assert c.metadata["doc_id"] == 42

    def test_start_end_word_positions(self):
        chunker = TextChunker(chunk_size=100, chunk_overlap=0, min_chunk_size=5)
        text = _make_long_text(250, paragraph_size=100)
        chunks = chunker.chunk(text)
        for c in chunks:
            assert c.end_word > c.start_word
            assert _word_count(c.text) == c.end_word - c.start_word


# ---------------------------------------------------------------------------
# Tests: chunk_by_sections()
# ---------------------------------------------------------------------------


class TestChunkBySections:
    def test_markdown_headers(self):
        text = (
            "# Introduction\n\n"
            "This is the introduction section with enough words to pass minimum.\n"
            "We need to add more words here to make sure it passes the minimum chunk size threshold easily.\n\n"
            "# Methods\n\n"
            "This section describes the methods used in the study and provides enough detail.\n"
            "Additional content to ensure the section is large enough for the minimum chunk size.\n\n"
            "# Results\n\n"
            "The results show significant improvements across all metrics tested in detail.\n"
            "More words here to ensure we pass the minimum chunk size threshold for this section too.\n"
        )
        chunker = TextChunker(chunk_size=512, min_chunk_size=10)
        chunks = chunker.chunk_by_sections(text)
        assert len(chunks) == 3
        # First chunk should contain introduction content
        assert "introduction" in chunks[0].text.lower()
        # Check section heading in metadata
        assert chunks[0].metadata.get("section_heading") == "Introduction"

    def test_all_caps_heading(self):
        text = (
            "OVERVIEW\n\n"
            "This is the overview section with enough words to meet the minimum.\n"
            "Additional words to ensure we pass the minimum chunk size threshold.\n\n"
            "DETAILS\n\n"
            "Here are the detailed findings from our comprehensive analysis.\n"
            "More content to ensure this section also passes the minimum chunk size.\n"
        )
        chunker = TextChunker(chunk_size=512, min_chunk_size=10)
        chunks = chunker.chunk_by_sections(text)
        assert len(chunks) == 2

    def test_large_section_falls_back_to_word_chunking(self):
        text = "# Big Section\n\n" + _make_long_text(1000)
        chunker = TextChunker(chunk_size=200, chunk_overlap=20, min_chunk_size=20)
        chunks = chunker.chunk_by_sections(text)
        assert len(chunks) > 1

    def test_empty_text(self):
        chunker = TextChunker()
        assert chunker.chunk_by_sections("") == []
        assert chunker.chunk_by_sections("   ") == []
