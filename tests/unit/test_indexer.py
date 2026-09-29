"""Tests for DocumentIndexer multi-target indexing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from interlock.worker.indexer import DocumentIndexer
from interlock.worker.summarizer import DocumentSummary


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    return pool


@pytest.fixture
def mock_entity_manager():
    mgr = AsyncMock()
    mgr.store_entities = AsyncMock(return_value=3)
    return mgr


@pytest.fixture
def mock_category_manager():
    mgr = AsyncMock()
    mgr.create_category = AsyncMock(return_value=1)
    mgr.assign_document = AsyncMock()
    mgr.increment_count = AsyncMock()
    return mgr


@pytest.fixture
def mock_semantic_index():
    idx = AsyncMock()
    idx.upsert = AsyncMock()
    return idx


@pytest.fixture
def mock_embedding_engine():
    engine = AsyncMock()
    engine.available = True
    engine.embed = AsyncMock(return_value=[0.1] * 384)
    return engine


@pytest.fixture
def sample_summary():
    return DocumentSummary(
        title="Test Document",
        doc_type="report",
        summary="A test document about testing.",
        topics=["testing", "documents"],
        entities=["Alice", "Bob", "Acme Corp"],
        category_path="engineering.testing",
        time_relevance="current",
        audience="engineers",
        quality_signals={"completeness": 0.8, "structure": 0.9},
    )


class TestDocumentIndexer:
    """Tests for DocumentIndexer.index_document."""

    @pytest.mark.asyncio
    async def test_upsert_discovery_asset(self, mock_pool, sample_summary):
        """Test that index_document upserts into discovery_assets."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        indexer = DocumentIndexer(pg_pool=mock_pool)
        asset_id = await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Some test content for searching.",
            summary=sample_summary,
        )

        assert asset_id == 42

        # Verify the upsert was called
        call_args = mock_pool.fetchrow.call_args
        sql = call_args[0][0]
        assert "INSERT INTO discovery_assets" in sql
        assert "ON CONFLICT" in sql
        assert call_args[0][1] == "src-1"  # source_id
        assert call_args[0][2] == "file"  # asset_type
        assert call_args[0][3] == "/docs/test.pdf"  # asset_path
        assert call_args[0][4] == "Test Document"  # title

    @pytest.mark.asyncio
    async def test_search_vector_updated(self, mock_pool, sample_summary):
        """Test that tsvector is updated after upsert."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        indexer = DocumentIndexer(pg_pool=mock_pool)
        await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Content for full text search.",
            summary=sample_summary,
        )

        # Find the search vector update call
        execute_calls = mock_pool.execute.call_args_list
        search_vector_calls = [c for c in execute_calls if "search_vector" in str(c)]
        assert len(search_vector_calls) == 1
        sql = search_vector_calls[0][0][0]
        assert "to_tsvector" in sql

    @pytest.mark.asyncio
    async def test_entity_storage(self, mock_pool, mock_entity_manager, sample_summary):
        """Test entity storage called with correct entities."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        indexer = DocumentIndexer(pg_pool=mock_pool, entity_manager=mock_entity_manager)
        await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Some content.",
            summary=sample_summary,
        )

        mock_entity_manager.store_entities.assert_awaited_once()
        call_args = mock_entity_manager.store_entities.call_args
        assert call_args[0][0] == 42  # document_id
        entities = call_args[0][1]
        assert len(entities) == 3
        entity_texts = [e["text"] for e in entities]
        assert "Alice" in entity_texts
        assert "Bob" in entity_texts
        assert "Acme Corp" in entity_texts

    @pytest.mark.asyncio
    async def test_entity_storage_skipped_when_no_entities(self, mock_pool, mock_entity_manager):
        """Test entity storage is skipped when summary has no entities."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        summary = DocumentSummary(title="No entities here")
        indexer = DocumentIndexer(pg_pool=mock_pool, entity_manager=mock_entity_manager)
        await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Content.",
            summary=summary,
        )

        mock_entity_manager.store_entities.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_category_creation(self, mock_pool, mock_category_manager, sample_summary):
        """Test category creation for new categories."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        indexer = DocumentIndexer(pg_pool=mock_pool, category_manager=mock_category_manager)
        await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Content.",
            summary=sample_summary,
        )

        mock_category_manager.create_category.assert_awaited_once()
        call_args = mock_category_manager.create_category.call_args
        assert call_args[1]["path"] == "engineering.testing"

        mock_category_manager.assign_document.assert_awaited_once_with(42, "engineering.testing")
        mock_category_manager.increment_count.assert_awaited_once_with("engineering.testing")

    @pytest.mark.asyncio
    async def test_category_skipped_when_no_path(self, mock_pool, mock_category_manager):
        """Test category assignment is skipped when no category_path."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        summary = DocumentSummary(title="No category")
        indexer = DocumentIndexer(pg_pool=mock_pool, category_manager=mock_category_manager)
        await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Content.",
            summary=summary,
        )

        mock_category_manager.create_category.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_embedding_stored_in_semantic_index(
        self, mock_pool, mock_semantic_index, sample_summary
    ):
        """Test embedding stored in FAISS semantic index."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        embedding = [0.1, 0.2, 0.3]
        indexer = DocumentIndexer(pg_pool=mock_pool, semantic_index=mock_semantic_index)
        await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Content.",
            summary=sample_summary,
            embedding=embedding,
        )

        mock_semantic_index.upsert.assert_awaited_once()
        call_args = mock_semantic_index.upsert.call_args
        assert call_args[1]["key"] == "src-1:/docs/test.pdf"
        assert call_args[1]["embedding"] == embedding
        assert call_args[1]["metadata"]["asset_id"] == 42
        assert call_args[1]["metadata"]["source_id"] == "src-1"
        assert call_args[1]["metadata"]["asset_path"] == "/docs/test.pdf"
        assert call_args[1]["metadata"]["asset_type"] == "file"
        assert call_args[1]["metadata"]["title"] == "Test Document"
        assert call_args[1]["metadata"]["summary"] == "A test document about testing."
        assert call_args[1]["metadata"]["doc_type"] == "report"

    @pytest.mark.asyncio
    async def test_embedding_skipped_when_none(
        self, mock_pool, mock_semantic_index, sample_summary
    ):
        """Test FAISS storage is skipped when no embedding provided."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        indexer = DocumentIndexer(pg_pool=mock_pool, semantic_index=mock_semantic_index)
        await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Content.",
            summary=sample_summary,
            embedding=None,
        )

        mock_semantic_index.upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quality_score_computed(self, mock_pool, sample_summary):
        """Test quality score is computed from quality signals."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        indexer = DocumentIndexer(pg_pool=mock_pool)
        await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Content.",
            summary=sample_summary,
        )

        # quality_score should be (0.8 + 0.9) / 2 = 0.85
        call_args = mock_pool.fetchrow.call_args
        quality_score = call_args[0][8]  # 9th positional arg
        assert quality_score == 0.85

    @pytest.mark.asyncio
    async def test_all_targets_optional(self, mock_pool):
        """Test that indexer works with no optional managers."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 1})
        mock_pool.execute = AsyncMock()

        indexer = DocumentIndexer(pg_pool=mock_pool)
        summary = DocumentSummary(title="Minimal")
        asset_id = await indexer.index_document(
            source_id="src-1",
            asset_path="/test.txt",
            asset_type="file",
            content="Hello",
            summary=summary,
        )
        assert asset_id == 1

    @pytest.mark.asyncio
    async def test_entity_error_does_not_block(self, mock_pool, sample_summary):
        """Test that entity storage errors are caught and do not block indexing."""
        mock_pool.fetchrow = AsyncMock(return_value={"id": 42})
        mock_pool.execute = AsyncMock()

        entity_mgr = AsyncMock()
        entity_mgr.store_entities = AsyncMock(side_effect=RuntimeError("DB error"))

        indexer = DocumentIndexer(pg_pool=mock_pool, entity_manager=entity_mgr)
        # Should not raise
        asset_id = await indexer.index_document(
            source_id="src-1",
            asset_path="/docs/test.pdf",
            asset_type="file",
            content="Content.",
            summary=sample_summary,
        )
        assert asset_id == 42
