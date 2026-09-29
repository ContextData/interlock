"""Tests for the EmbeddingEngine.

Tests graceful degradation when sentence-transformers is not installed,
and actual embedding quality when it is available.
"""

from __future__ import annotations

import math
import sys
from unittest.mock import patch

import pytest

from interlock.cache.embedding import EmbeddingEngine

# Detect whether sentence-transformers is actually importable
_ST_AVAILABLE = False
try:
    import sentence_transformers  # noqa: F401

    _ST_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Degraded mode tests (always run - mock away sentence_transformers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_without_sentence_transformers():
    """When sentence-transformers is not importable, _available stays False."""
    engine = EmbeddingEngine()

    with patch.dict(sys.modules, {"sentence_transformers": None}):
        await engine.initialize()

    assert engine.available is False


@pytest.mark.asyncio
async def test_embed_when_unavailable():
    """embed() returns an empty list when the engine is not available."""
    engine = EmbeddingEngine()
    # Do not call initialize - stays unavailable
    result = await engine.embed("hello world")
    assert result == []


@pytest.mark.asyncio
async def test_embed_batch_when_unavailable():
    """embed_batch() returns a list of empty lists when unavailable."""
    engine = EmbeddingEngine()
    texts = ["hello", "world", "test"]
    result = await engine.embed_batch(texts)
    assert result == [[], [], []]


def test_dimension_property():
    """dimension property returns 384 regardless of availability."""
    engine = EmbeddingEngine()
    assert engine.dimension == 384


@pytest.mark.asyncio
async def test_shutdown_safe_when_not_initialized():
    """shutdown() does not raise when called before initialize()."""
    engine = EmbeddingEngine()
    await engine.shutdown()  # should not raise
    assert engine.available is False


@pytest.mark.asyncio
async def test_available_false_by_default():
    """A freshly constructed engine is not available."""
    engine = EmbeddingEngine()
    assert engine.available is False


# ---------------------------------------------------------------------------
# Live model tests (only run when sentence-transformers is installed)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _ST_AVAILABLE, reason="sentence-transformers not installed")
@pytest.mark.asyncio
async def test_embed_returns_384_dim():
    """embed() returns a 384-dimensional vector."""
    engine = EmbeddingEngine()
    await engine.initialize()
    try:
        assert engine.available is True
        vec = await engine.embed("The quick brown fox")
        assert len(vec) == 384
        assert all(isinstance(v, float) for v in vec)
    finally:
        await engine.shutdown()


@pytest.mark.skipif(not _ST_AVAILABLE, reason="sentence-transformers not installed")
@pytest.mark.asyncio
async def test_embed_batch_returns_correct_count():
    """embed_batch() returns one vector per input text."""
    engine = EmbeddingEngine()
    await engine.initialize()
    try:
        texts = ["hello", "world", "test sentence"]
        vecs = await engine.embed_batch(texts)
        assert len(vecs) == 3
        for vec in vecs:
            assert len(vec) == 384
    finally:
        await engine.shutdown()


@pytest.mark.skipif(not _ST_AVAILABLE, reason="sentence-transformers not installed")
@pytest.mark.asyncio
async def test_embeddings_are_normalized():
    """Embeddings should have L2 norm approximately 1.0."""
    engine = EmbeddingEngine()
    await engine.initialize()
    try:
        vec = await engine.embed("Normalization check")
        norm = math.sqrt(sum(v * v for v in vec))
        assert abs(norm - 1.0) < 1e-3, f"L2 norm was {norm}, expected ~1.0"
    finally:
        await engine.shutdown()


@pytest.mark.skipif(not _ST_AVAILABLE, reason="sentence-transformers not installed")
@pytest.mark.asyncio
async def test_similar_texts_high_cosine_similarity():
    """Semantically similar texts should have high cosine similarity."""
    engine = EmbeddingEngine()
    await engine.initialize()
    try:
        vec_a = await engine.embed("The cat sat on the mat")
        vec_b = await engine.embed("A cat is sitting on a rug")
        vec_c = await engine.embed("Quantum physics explains particle behavior")

        def cosine_sim(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(x * x for x in b))
            return dot / (norm_a * norm_b)

        sim_ab = cosine_sim(vec_a, vec_b)
        sim_ac = cosine_sim(vec_a, vec_c)

        # Similar texts should score higher than dissimilar ones
        assert sim_ab > sim_ac, (
            f"Similar texts similarity ({sim_ab:.3f}) should exceed "
            f"dissimilar texts similarity ({sim_ac:.3f})"
        )
        # Similar cat sentences should have reasonably high similarity
        assert sim_ab > 0.5, f"Similar texts cosine sim was {sim_ab:.3f}, expected > 0.5"
    finally:
        await engine.shutdown()
