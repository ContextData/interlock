"""Tests for ChunkIndexer - chunk-level semantic indexing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from interlock.models import Chunk
from interlock.worker.chunk_indexer import ChunkIndexer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_faiss():
    idx = AsyncMock()
    idx.upsert = AsyncMock()
    return idx


@pytest.fixture
def mock_embedding():
    engine = AsyncMock()
    engine.available = True
    engine.embed_batch = AsyncMock(return_value=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    return engine


@pytest.fixture
def sample_chunks():
    return [
        Chunk(text="First chunk of text with content", index=0, start_word=0, end_word=6),
        Chunk(text="Second chunk of text with more content", index=1, start_word=5, end_word=12),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestChunkIndexer:
    @pytest.mark.asyncio
    async def test_index_chunks_embeds_each(self, mock_faiss, mock_embedding, sample_chunks):
        indexer = ChunkIndexer(
            semantic_index=mock_faiss,
            embedding_engine=mock_embedding,
        )
        count = await indexer.index_chunks(
            document_id=42,
            source_id="src_1",
            chunks=sample_chunks,
        )
        assert count == 2
        # embed_batch should have been called once with both texts
        mock_embedding.embed_batch.assert_called_once_with(
            ["First chunk of text with content", "Second chunk of text with more content"]
        )
        # upsert should have been called for each chunk
        assert mock_faiss.upsert.call_count == 2

    @pytest.mark.asyncio
    async def test_key_format(self, mock_faiss, mock_embedding, sample_chunks):
        indexer = ChunkIndexer(
            semantic_index=mock_faiss,
            embedding_engine=mock_embedding,
        )
        await indexer.index_chunks(
            document_id=42,
            source_id="src_1",
            chunks=sample_chunks,
        )
        # Check key format for first call
        first_call = mock_faiss.upsert.call_args_list[0]
        assert first_call.kwargs["key"] == "src_1:42:chunk:0"
        second_call = mock_faiss.upsert.call_args_list[1]
        assert second_call.kwargs["key"] == "src_1:42:chunk:1"

    @pytest.mark.asyncio
    async def test_metadata_includes_document_and_chunk(
        self, mock_faiss, mock_embedding, sample_chunks
    ):
        indexer = ChunkIndexer(
            semantic_index=mock_faiss,
            embedding_engine=mock_embedding,
        )
        await indexer.index_chunks(
            document_id=42,
            source_id="src_1",
            chunks=sample_chunks,
        )
        first_call = mock_faiss.upsert.call_args_list[0]
        meta = first_call.kwargs["metadata"]
        assert meta["document_id"] == 42
        assert meta["chunk_index"] == 0
        assert meta["source_id"] == "src_1"
        assert "first_words" in meta

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_zero(self, mock_faiss, mock_embedding):
        indexer = ChunkIndexer(
            semantic_index=mock_faiss,
            embedding_engine=mock_embedding,
        )
        count = await indexer.index_chunks(document_id=1, source_id="s", chunks=[])
        assert count == 0

    @pytest.mark.asyncio
    async def test_no_engine_returns_zero(self, sample_chunks):
        indexer = ChunkIndexer(semantic_index=None, embedding_engine=None)
        count = await indexer.index_chunks(document_id=1, source_id="s", chunks=sample_chunks)
        assert count == 0

    @pytest.mark.asyncio
    async def test_chunk_metadata_merged(self, mock_faiss, mock_embedding):
        """Chunk-level metadata should be merged into the indexed metadata."""
        chunks = [
            Chunk(
                text="Hello world test chunk",
                index=0,
                start_word=0,
                end_word=4,
                metadata={"custom_key": "custom_val"},
            ),
        ]
        mock_embedding.embed_batch = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
        indexer = ChunkIndexer(
            semantic_index=mock_faiss,
            embedding_engine=mock_embedding,
        )
        await indexer.index_chunks(document_id=1, source_id="s", chunks=chunks)
        meta = mock_faiss.upsert.call_args_list[0].kwargs["metadata"]
        assert meta["custom_key"] == "custom_val"
