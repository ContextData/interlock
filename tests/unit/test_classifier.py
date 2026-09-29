"""Unit tests for CategoryClassifier."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from interlock.discovery.classifier import CategoryClassifier, _cosine_similarity

# ---------------------------------------------------------------------------
# cosine_similarity helper
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_zero_vector(self):
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.asyncio
    async def test_classify_without_engine_returns_empty(self):
        pool = AsyncMock()
        classifier = CategoryClassifier(pg_pool=pool, embedding_engine=None)
        result = await classifier.classify("test query")
        assert result == []

    @pytest.mark.asyncio
    async def test_classify_with_unavailable_engine_returns_empty(self):
        pool = AsyncMock()
        engine = MagicMock()
        type(engine).available = PropertyMock(return_value=False)

        classifier = CategoryClassifier(pg_pool=pool, embedding_engine=engine)
        result = await classifier.classify("test query")
        assert result == []

    @pytest.mark.asyncio
    async def test_classify_with_no_categories_returns_empty(self):
        pool = AsyncMock()
        engine = MagicMock()
        type(engine).available = PropertyMock(return_value=True)
        engine.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])

        classifier = CategoryClassifier(pg_pool=pool, embedding_engine=engine)
        # No categories loaded
        result = await classifier.classify("test query")
        assert result == []

    @pytest.mark.asyncio
    async def test_classify_returns_ranked_categories(self):
        pool = AsyncMock()
        engine = MagicMock()
        type(engine).available = PropertyMock(return_value=True)
        engine.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])

        classifier = CategoryClassifier(pg_pool=pool, embedding_engine=engine)
        # Manually set category embeddings
        classifier._category_embeddings = {
            "engineering.backend": [1.0, 0.0, 0.0],  # identical to query - highest
            "engineering.frontend": [0.7, 0.7, 0.0],  # moderate similarity
            "marketing": [0.0, 0.0, 1.0],  # orthogonal - lowest
        }

        result = await classifier.classify("test query", top_k=3)

        assert len(result) == 3
        # First result should be engineering.backend (highest similarity)
        assert result[0][0] == "engineering.backend"
        assert abs(result[0][1] - 1.0) < 1e-6
        # Last should be marketing (orthogonal)
        assert result[2][0] == "marketing"
        assert abs(result[2][1]) < 1e-6

    @pytest.mark.asyncio
    async def test_classify_respects_top_k(self):
        pool = AsyncMock()
        engine = MagicMock()
        type(engine).available = PropertyMock(return_value=True)
        engine.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])

        classifier = CategoryClassifier(pg_pool=pool, embedding_engine=engine)
        classifier._category_embeddings = {
            "a": [1.0, 0.0, 0.0],
            "b": [0.5, 0.5, 0.0],
            "c": [0.0, 1.0, 0.0],
        }

        result = await classifier.classify("query", top_k=1)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# load_categories
# ---------------------------------------------------------------------------


class TestLoadCategories:
    @pytest.mark.asyncio
    async def test_loads_from_pg(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {"path": "engineering", "embedding": [0.1, 0.2, 0.3]},
                {"path": "marketing", "embedding": [0.4, 0.5, 0.6]},
                {"path": "empty", "embedding": None},
            ]
        )

        classifier = CategoryClassifier(pg_pool=pool)
        await classifier.load_categories()

        assert len(classifier._category_embeddings) == 2
        assert "engineering" in classifier._category_embeddings
        assert "marketing" in classifier._category_embeddings
        assert "empty" not in classifier._category_embeddings
