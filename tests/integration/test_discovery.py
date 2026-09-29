"""End-to-end integration tests for Phase 4 discovery pipeline.

Tests the full pipeline without real PG, Redis, or external APIs.
Uses mocks for async PG pool, entity/category managers, and the
FAISS index with its pure-Python fallback for vector search.
"""

from __future__ import annotations

import math
import os
import tempfile
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.audit.cost import UsageAggregator
from interlock.cache.faiss_index import FAISSIndex
from interlock.discovery.enrichment import EntityEnricher
from interlock.discovery.entities import EntityManager
from interlock.models import SearchResult
from interlock.worker.extractors.base import (
    ExtractedContent,
    ExtractionRegistry,
)
from interlock.worker.indexer import DocumentIndexer
from interlock.worker.loop import IngestionWorker
from interlock.worker.sampler import ContentSampler
from interlock.worker.summarizer import DocumentSummary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_embedding(seed: float, dim: int = 384) -> list[float]:
    """Create a deterministic normalized embedding from a seed value.

    Produces a vector where most energy is concentrated in a few dimensions
    determined by `seed`, so different seeds produce distinct directions.
    """
    vec = [0.0] * dim
    base = int(seed * 10) % dim
    for i in range(dim):
        vec[i] = math.sin(seed * (i + 1)) + math.cos(seed * (base + i))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _make_mock_pool() -> AsyncMock:
    """Create a mock asyncpg.Pool with common methods stubbed."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock(return_value=None)
    pool.acquire = MagicMock()

    # Make acquire() usable as async context manager
    conn = AsyncMock()
    conn.execute = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = cm
    pool._conn = conn  # expose for test assertions

    return pool


def _make_summary(
    title: str = "Test Doc",
    doc_type: str = "documentation",
    summary: str = "A test document.",
    topics: list[str] | None = None,
    entities: list[str] | None = None,
    category_path: str = "",
    quality_signals: dict | None = None,
) -> DocumentSummary:
    return DocumentSummary(
        title=title,
        doc_type=doc_type,
        summary=summary,
        topics=topics or [],
        entities=entities or [],
        category_path=category_path,
        quality_signals=quality_signals or {"completeness": 0.8, "structure": 0.7},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pool():
    return _make_mock_pool()


@pytest.fixture
def faiss_index():
    """Create a real FAISSIndex using pure-Python fallback (no faiss-cpu needed)."""
    idx = FAISSIndex(dimension=384, namespace="test", redis_client=None)
    # Force pure-Python mode and mark available
    idx._use_faiss = False
    idx._available = True
    return idx


@pytest.fixture
def sampler():
    return ContentSampler()


# ---------------------------------------------------------------------------
# 1. test_full_ingestion_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_ingestion_pipeline(mock_pool, faiss_index):
    """Extract text from a temp file, sample it, generate a mock summary,
    index across all targets. Verify each target received data."""

    # Set up entity manager and category manager mocks
    entity_mgr = AsyncMock(spec=EntityManager)
    entity_mgr.store_entities = AsyncMock(return_value=3)

    category_mgr = AsyncMock()
    category_mgr.create_category = AsyncMock(return_value=1)
    category_mgr.assign_document = AsyncMock()
    category_mgr.increment_count = AsyncMock()

    # Mock PG upsert to return an asset ID
    mock_pool.fetchrow = AsyncMock(return_value={"id": 42})

    embedding = _make_embedding(1.0)

    indexer = DocumentIndexer(
        pg_pool=mock_pool,
        entity_manager=entity_mgr,
        category_manager=category_mgr,
        semantic_index=faiss_index,
        embedding_engine=None,
    )

    # Create a temp file with content
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("Machine learning models are transforming data analysis workflows.\n")
        f.write("This document covers neural networks, gradient descent, and backpropagation.\n")
        tmp_path = f.name

    try:
        # Step 1: Extract using the text extractor
        registry = ExtractionRegistry()
        from interlock.worker.extractors.text import TextExtractor

        registry.register(TextExtractor())
        extractor = registry.get_extractor(tmp_path)
        assert extractor is not None
        extracted = await extractor.extract(tmp_path)
        assert "Machine learning" in extracted.text

        # Step 2: Sample
        sampler = ContentSampler()
        sampled = sampler.sample(extracted.text)
        assert sampled.strategy == "full"
        assert sampled.sampled_word_count > 0

        # Step 3: Mock summary
        summary = _make_summary(
            title="ML Overview",
            topics=["machine learning", "neural networks"],
            entities=["gradient descent", "backpropagation"],
            category_path="data_science.ml",
        )

        # Step 4: Index
        asset_id = await indexer.index_document(
            source_id="src-1",
            asset_path=tmp_path,
            asset_type="file",
            content=extracted.text,
            summary=summary,
            embedding=embedding,
        )

        assert asset_id == 42

        # Verify PG upsert was called
        assert mock_pool.fetchrow.call_count >= 1

        # Verify tsvector update was called
        assert mock_pool.execute.call_count >= 1

        # Verify entity storage was called
        entity_mgr.store_entities.assert_awaited_once()
        call_args = entity_mgr.store_entities.call_args
        assert call_args[0][0] == 42  # document_id
        assert len(call_args[0][1]) == 2  # 2 entities

        # Verify category was assigned
        category_mgr.create_category.assert_awaited_once()
        category_mgr.assign_document.assert_awaited_once_with(42, "data_science.ml")
        category_mgr.increment_count.assert_awaited_once_with("data_science.ml")

        # Verify FAISS index received the embedding
        await faiss_index.rebuild()
        count = await faiss_index.count()
        assert count == 1

    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# 2. test_document_search_after_indexing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_search_after_indexing(faiss_index):
    """Index documents with different topics, search, verify ranking."""

    # Pre-populate the FAISS index with 3 documents of different topics
    docs = [
        (
            "src1:ml_paper.txt",
            _make_embedding(1.0),
            {
                "asset_id": 1,
                "source_id": "src1",
                "asset_type": "file",
                "title": "Machine Learning Primer",
                "asset_path": "ml_paper.txt",
            },
        ),
        (
            "src1:cooking.txt",
            _make_embedding(2.0),
            {
                "asset_id": 2,
                "source_id": "src1",
                "asset_type": "file",
                "title": "Italian Cooking Guide",
                "asset_path": "cooking.txt",
            },
        ),
        (
            "src1:ml_advanced.txt",
            _make_embedding(1.1),
            {
                "asset_id": 3,
                "source_id": "src1",
                "asset_type": "file",
                "title": "Advanced ML Techniques",
                "asset_path": "ml_advanced.txt",
            },
        ),
    ]

    for key, emb, meta in docs:
        await faiss_index.upsert(key=key, embedding=emb, metadata=meta)
    await faiss_index.rebuild()

    # Search with an embedding close to seed=1.0 (ML topic)
    query_embedding = _make_embedding(1.05)
    results = await faiss_index.search(query_embedding, top_k=3)

    assert len(results) == 3

    # The two ML docs (seeds 1.0 and 1.1) should score higher than cooking (seed 2.0)
    ml_ids = {1, 3}
    top_two_ids = {r.metadata["asset_id"] for r in results[:2]}
    assert ml_ids == top_two_ids, f"Expected ML docs in top 2, got {top_two_ids}"


# ---------------------------------------------------------------------------
# 3. test_entity_enrichment_on_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_enrichment_on_search():
    """Index documents with entities, search, enrich results.
    Verify entity metadata attached and scores boosted."""

    entity_mgr = AsyncMock(spec=EntityManager)

    # Document 1 has entity "python", document 2 does not
    async def fake_get_entities(doc_id):
        if doc_id == 1:
            return [
                {"entity_text": "python", "entity_type": "technology", "prominence": 0.9},
                {"entity_text": "tensorflow", "entity_type": "technology", "prominence": 0.7},
            ]
        elif doc_id == 2:
            return [
                {"entity_text": "javascript", "entity_type": "technology", "prominence": 0.8},
            ]
        return []

    entity_mgr.get_entities_for_document = AsyncMock(side_effect=fake_get_entities)

    enricher = EntityEnricher(entity_mgr)

    # Simulate search results
    base_results = [
        SearchResult(
            asset_id=1,
            source_id="s1",
            asset_type="file",
            asset_path="a.py",
            title="Python Tutorial",
            score=5.0,
            strategy="fulltext",
        ),
        SearchResult(
            asset_id=2,
            source_id="s1",
            asset_type="file",
            asset_path="b.js",
            title="JS Guide",
            score=4.5,
            strategy="fulltext",
        ),
    ]

    # Query mentions "python" entity
    enriched = await enricher.enrich_results(base_results, query_entities=["Python"])

    assert len(enriched) == 2

    # Document 1 should be boosted (shares "python" entity with query)
    doc1 = enriched[0]
    assert doc1.asset_id == 1
    assert doc1.score == 5.0 * 1.5  # ENTITY_BOOST
    assert "entities" in doc1.metadata
    assert len(doc1.metadata["entities"]) == 2
    assert "shared_entities" in doc1.metadata
    assert "python" in doc1.metadata["shared_entities"]

    # Document 2 should NOT be boosted
    doc2 = enriched[1]
    assert doc2.asset_id == 2
    assert doc2.score == 4.5  # unchanged
    assert "entities" in doc2.metadata


# ---------------------------------------------------------------------------
# 4. test_category_scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_scoping(faiss_index):
    """Index documents in different categories. Search with source filter.
    Verify only matching source returned."""

    # Add docs with different source_ids to simulate category-like filtering via metadata
    await faiss_index.upsert(
        key="eng:api_docs.md",
        embedding=_make_embedding(3.0),
        metadata={
            "asset_id": 10,
            "source_id": "engineering",
            "asset_type": "file",
            "title": "API Documentation",
            "category": "engineering.backend",
        },
    )
    await faiss_index.upsert(
        key="sales:pipeline.md",
        embedding=_make_embedding(3.1),
        metadata={
            "asset_id": 11,
            "source_id": "sales",
            "asset_type": "file",
            "title": "Sales Pipeline",
            "category": "sales.pipeline",
        },
    )
    await faiss_index.upsert(
        key="eng:design.md",
        embedding=_make_embedding(3.2),
        metadata={
            "asset_id": 12,
            "source_id": "engineering",
            "asset_type": "file",
            "title": "System Design",
            "category": "engineering.architecture",
        },
    )
    await faiss_index.rebuild()

    query_emb = _make_embedding(3.05)

    # Search with source_id filter for "engineering" only
    results = await faiss_index.search(query_emb, top_k=10, filters={"source_id": "engineering"})

    # Should only return engineering docs
    assert len(results) == 2
    result_ids = {r.metadata["asset_id"] for r in results}
    assert result_ids == {10, 12}

    # Search with source_id filter for "sales"
    results_sales = await faiss_index.search(query_emb, top_k=10, filters={"source_id": "sales"})
    assert len(results_sales) == 1
    assert results_sales[0].metadata["asset_id"] == 11


# ---------------------------------------------------------------------------
# 5. test_content_sampler_strategies
# ---------------------------------------------------------------------------


def test_content_sampler_strategies(sampler):
    """Test all three sampling strategies produce correct output."""

    # Small document (<2500 words) - full strategy
    small_text = "This is a small document. " * 100  # ~600 words
    result_small = sampler.sample(small_text)
    assert result_small.strategy == "full"
    assert result_small.sampling_ratio == 1.0
    assert result_small.text == small_text

    # Medium document (2500-25000 words) - strategic strategy
    medium_text = "\n\n".join(
        [f"Paragraph {i}. " + "Word " * 50 for i in range(100)]
    )  # ~5100 words
    result_medium = sampler.sample(medium_text)
    assert result_medium.strategy == "strategic"
    assert 0.0 < result_medium.sampling_ratio < 1.0
    assert result_medium.sampled_word_count < result_medium.original_word_count
    # Strategic sample should include first and last paragraphs
    assert "Paragraph 0." in result_medium.text
    assert "Paragraph 99." in result_medium.text

    # Large document (>25000 words) - structure strategy
    sections = []
    for i in range(50):
        sections.append(f"# Section {i}")
        sections.append(f"This section covers topic {i} in great detail. " + "Filler word. " * 600)
    large_text = "\n".join(sections)
    result_large = sampler.sample(large_text)
    assert result_large.strategy == "structure"
    assert result_large.sampling_ratio < 0.5
    assert result_large.sampled_word_count < result_large.original_word_count
    # Structure sample should contain headings
    assert "# Section 0" in result_large.text

    # Empty document
    result_empty = sampler.sample("")
    assert result_empty.strategy == "full"
    assert result_empty.original_word_count == 0
    assert result_empty.sampling_ratio == 1.0


# ---------------------------------------------------------------------------
# 6. test_extraction_registry_selects_correct_extractor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_registry_selects_correct_extractor():
    """Register multiple extractors, verify correct one selected by file extension."""

    class FakePDFExtractor:
        def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
            return file_path.endswith(".pdf")

        async def extract(self, file_path: str) -> ExtractedContent:
            return ExtractedContent(text="PDF content", metadata={"type": "pdf"})

    class FakeCSVExtractor:
        def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
            return file_path.endswith(".csv")

        async def extract(self, file_path: str) -> ExtractedContent:
            return ExtractedContent(text="CSV content", metadata={"type": "csv"})

    class FakeTxtExtractor:
        def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
            return file_path.endswith(".txt")

        async def extract(self, file_path: str) -> ExtractedContent:
            return ExtractedContent(text="TXT content", metadata={"type": "txt"})

    registry = ExtractionRegistry()
    registry.register(FakePDFExtractor())
    registry.register(FakeCSVExtractor())
    registry.register(FakeTxtExtractor())

    # PDF file should pick PDF extractor
    ext = registry.get_extractor("/data/report.pdf")
    assert ext is not None
    result = await ext.extract("/data/report.pdf")
    assert result.metadata["type"] == "pdf"

    # CSV file should pick CSV extractor
    ext = registry.get_extractor("/data/sales.csv")
    assert ext is not None
    result = await ext.extract("/data/sales.csv")
    assert result.metadata["type"] == "csv"

    # TXT file should pick TXT extractor
    ext = registry.get_extractor("/data/readme.txt")
    assert ext is not None
    result = await ext.extract("/data/readme.txt")
    assert result.metadata["type"] == "txt"

    # Unknown extension returns None
    ext = registry.get_extractor("/data/archive.zip")
    assert ext is None

    # Verify registration count
    assert len(registry.extractors) == 3


# ---------------------------------------------------------------------------
# 7. test_worker_pipeline_end_to_end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_pipeline_end_to_end():
    """Simulate a complete worker cycle: create a fake job, run _process_job,
    verify job processed and document indexed."""

    mock_pool = _make_mock_pool()

    # Create a temp file for extraction
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("Integration test document about database optimization.\n")
        f.write("Covers indexing strategies, query planning, and caching.\n")
        tmp_path = f.name

    try:
        # Set up extraction registry with real text extractor
        registry = ExtractionRegistry()
        from interlock.worker.extractors.text import TextExtractor

        registry.register(TextExtractor())

        # Mock summarizer
        mock_summarizer = AsyncMock()
        mock_summarizer.summarize = AsyncMock(
            return_value=_make_summary(
                title="Database Optimization",
                topics=["database", "indexing", "caching"],
                entities=["PostgreSQL"],
                category_path="engineering.database",
            )
        )

        # Mock indexer
        mock_indexer = AsyncMock(spec=DocumentIndexer)
        mock_indexer.index_document = AsyncMock(return_value=99)
        mock_indexer._embedding_engine = None

        worker = IngestionWorker(
            worker_id="test-worker-1",
            pg_pool=mock_pool,
            redis_client=None,
            extraction_registry=registry,
            sampler=ContentSampler(),
            summarizer=mock_summarizer,
            indexer=mock_indexer,
        )

        # Simulate a job dict (as returned from PG claim)
        job = {
            "id": 501,
            "file_path": tmp_path,
            "source_id": "src-test",
            "metadata": '{"asset_type": "file"}',
            "priority_score": 1.0,
            "status": "processing",
        }

        # Run the pipeline
        await worker._process_job(job)

        # Verify summarizer was called with sampled content
        mock_summarizer.summarize.assert_awaited_once()
        call_kwargs = mock_summarizer.summarize.call_args
        assert (
            "database optimization"
            in call_kwargs.kwargs.get(
                "content",
                call_kwargs[1].get("content", call_kwargs[0][0] if call_kwargs[0] else ""),
            ).lower()
            or "database optimization" in str(call_kwargs).lower()
        )

        # Verify indexer was called
        mock_indexer.index_document.assert_awaited_once()
        idx_kwargs = mock_indexer.index_document.call_args
        # Check source_id was passed through
        assert idx_kwargs.kwargs.get("source_id") == "src-test" or (
            idx_kwargs[1].get("source_id") == "src-test" if len(idx_kwargs) > 1 else False
        )

    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# 8. test_usage_aggregation_queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_aggregation_queries():
    """The usage aggregator counts requests by outcome for a window."""

    mock_pool = _make_mock_pool()
    aggregator = UsageAggregator(mock_pool)
    since = datetime(2026, 9, 18, tzinfo=UTC)

    mock_pool.fetch = AsyncMock(
        return_value=[
            {
                "identity_id": 1,
                "identity_name": "a",
                "total": 100,
                "upstream": 60,
                "cached": 30,
                "denied": 10,
                "queued": 0,
                "errors": 0,
            },
        ]
    )
    rows = await aggregator.by_identity(since)
    assert rows[0]["total"] == 100
    assert mock_pool.fetch.call_args[0][1] == since

    mock_pool.fetch = AsyncMock(return_value=[{"source_id": "pg-prod", "total": 200}])
    assert (await aggregator.by_source(since))[0]["source_id"] == "pg-prod"

    mock_pool.fetch = AsyncMock(return_value=[{"team": None, "total": 5}])
    assert (await aggregator.by_team(since))[0]["team"] is None

    mock_pool.fetchrow = AsyncMock(
        return_value={
            "total": 500,
            "upstream": 300,
            "cached": 100,
            "denied": 80,
            "queued": 10,
            "errors": 10,
            "avg_latency_ms": 42.5,
        }
    )
    summary = await aggregator.summary(since)
    assert summary["total"] == 500
    assert summary["cache_hit_rate"] == pytest.approx(100 / 400)

    mock_pool.fetchrow = AsyncMock(return_value=None)
    summary = await aggregator.summary(since)
    assert summary["total"] == 0 and summary["cache_hit_rate"] == 0.0
