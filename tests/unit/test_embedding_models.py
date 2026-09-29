"""Tests for EmbeddingEngine model configuration and the embedding_model
config fields in SemanticCacheConfig.

Validates dynamic dimension detection, model_info property, ONNX fallback
logic, and config integration.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from interlock.cache.embedding import (
    _KNOWN_DIMENSIONS,
    DEFAULT_DIMENSION,
    DEFAULT_MODEL,
    EmbeddingEngine,
)
from interlock.config import SemanticCacheConfig

# ---------------------------------------------------------------------------
# Default model configuration
# ---------------------------------------------------------------------------


def test_default_model_name():
    """Engine should default to all-MiniLM-L6-v2."""
    engine = EmbeddingEngine()
    assert engine._model_name == DEFAULT_MODEL
    assert engine._model_name == "all-MiniLM-L6-v2"


def test_default_dimension():
    """Default dimension should be 384 (MiniLM-L6-v2)."""
    engine = EmbeddingEngine()
    assert engine.dimension == 384


def test_custom_model_name():
    """Engine should accept a custom model name."""
    engine = EmbeddingEngine(model_name="all-mpnet-base-v2")
    assert engine._model_name == "all-mpnet-base-v2"


# ---------------------------------------------------------------------------
# Dimension property
# ---------------------------------------------------------------------------


def test_dimension_from_known_models():
    """Dimension should be looked up from known models table before init."""
    engine_384 = EmbeddingEngine(model_name="all-MiniLM-L6-v2")
    assert engine_384.dimension == 384

    engine_768 = EmbeddingEngine(model_name="all-mpnet-base-v2")
    assert engine_768.dimension == 768

    engine_bge = EmbeddingEngine(model_name="BAAI/bge-small-en-v1.5")
    assert engine_bge.dimension == 384


def test_dimension_unknown_model_uses_default():
    """Unknown models should get the default dimension before init."""
    engine = EmbeddingEngine(model_name="custom/unknown-model")
    assert engine.dimension == DEFAULT_DIMENSION


@pytest.mark.asyncio
async def test_deterministic_test_embedding_mode():
    """Deterministic mode should not require sentence-transformers."""
    engine = EmbeddingEngine(model_name="deterministic-test")
    await engine.initialize()

    first = await engine.embed("same text")
    second = await engine.embed("same text")
    other = await engine.embed("other text")

    assert engine.available is True
    assert len(first) == DEFAULT_DIMENSION
    assert first == second
    assert first != other


@pytest.mark.asyncio
async def test_dimension_detected_from_loaded_model():
    """After initialization, dimension should be detected from the model."""
    mock_st = MagicMock()
    mock_model = MagicMock()
    mock_model.get_sentence_embedding_dimension.return_value = 512

    mock_st.SentenceTransformer.return_value = mock_model

    engine = EmbeddingEngine(model_name="custom/512-model")

    with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
        await engine.initialize()

    assert engine.dimension == 512
    assert engine.available is True


@pytest.mark.asyncio
async def test_dimension_fallback_when_detection_fails():
    """If model doesn't expose dimension, fall back to known table."""
    mock_st = MagicMock()
    mock_model = MagicMock()
    mock_model.get_sentence_embedding_dimension.side_effect = AttributeError

    mock_st.SentenceTransformer.return_value = mock_model

    engine = EmbeddingEngine(model_name="all-mpnet-base-v2")

    with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
        await engine.initialize()

    assert engine.dimension == 768  # from known table


# ---------------------------------------------------------------------------
# model_info property
# ---------------------------------------------------------------------------


def test_model_info_before_init():
    """model_info should return correct data before initialization."""
    engine = EmbeddingEngine(model_name="all-mpnet-base-v2", use_onnx=True)
    info = engine.model_info

    assert info["model_name"] == "all-mpnet-base-v2"
    assert info["dimension"] == 768
    assert info["use_onnx"] is True
    assert info["available"] is False


@pytest.mark.asyncio
async def test_model_info_after_init():
    """model_info should reflect availability after initialization."""
    mock_st = MagicMock()
    mock_model = MagicMock()
    mock_model.get_sentence_embedding_dimension.return_value = 384
    mock_st.SentenceTransformer.return_value = mock_model

    engine = EmbeddingEngine()

    with patch.dict(sys.modules, {"sentence_transformers": mock_st}):
        await engine.initialize()

    info = engine.model_info
    assert info["available"] is True
    assert info["model_name"] == "all-MiniLM-L6-v2"
    assert info["dimension"] == 384


# ---------------------------------------------------------------------------
# ONNX support
# ---------------------------------------------------------------------------


def test_use_onnx_flag():
    """use_onnx flag should be stored on the engine."""
    engine = EmbeddingEngine(use_onnx=True)
    assert engine._use_onnx is True

    engine2 = EmbeddingEngine(use_onnx=False)
    assert engine2._use_onnx is False


@pytest.mark.asyncio
async def test_onnx_fallback_to_pytorch():
    """When ONNX is requested but optimum is not installed, fall back to PyTorch."""
    mock_st = MagicMock()
    mock_model = MagicMock()
    mock_model.get_sentence_embedding_dimension.return_value = 384
    mock_st.SentenceTransformer.return_value = mock_model

    engine = EmbeddingEngine(use_onnx=True)

    # Patch so optimum import fails inside _load_onnx_model
    with (
        patch.dict(sys.modules, {"sentence_transformers": mock_st}),
        patch.dict(
            sys.modules,
            {
                "optimum": None,
                "optimum.onnxruntime": None,
            },
        ),
    ):
        await engine.initialize()

    assert engine.available is True
    # Should have fallen back to standard SentenceTransformer
    mock_st.SentenceTransformer.assert_called()


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


def test_semantic_cache_config_defaults():
    """SemanticCacheConfig should have the new embedding fields with defaults."""
    cfg = SemanticCacheConfig()
    assert cfg.embedding_model == "all-MiniLM-L6-v2"
    assert cfg.embedding_dimension == 384
    assert cfg.use_onnx is False


def test_semantic_cache_config_custom_values():
    """SemanticCacheConfig should accept custom embedding values."""
    cfg = SemanticCacheConfig(
        embedding_model="all-mpnet-base-v2",
        embedding_dimension=768,
        use_onnx=True,
    )
    assert cfg.embedding_model == "all-mpnet-base-v2"
    assert cfg.embedding_dimension == 768
    assert cfg.use_onnx is True


def test_config_model_matches_engine_default():
    """Config default should match the EmbeddingEngine default."""
    cfg = SemanticCacheConfig()
    engine = EmbeddingEngine()
    assert cfg.embedding_model == engine._model_name
    assert cfg.embedding_dimension == engine.dimension


# ---------------------------------------------------------------------------
# Known dimensions table
# ---------------------------------------------------------------------------


def test_known_dimensions_table():
    """The known dimensions table should contain expected models."""
    assert "all-MiniLM-L6-v2" in _KNOWN_DIMENSIONS
    assert "all-mpnet-base-v2" in _KNOWN_DIMENSIONS
    assert "BAAI/bge-small-en-v1.5" in _KNOWN_DIMENSIONS
    assert _KNOWN_DIMENSIONS["all-MiniLM-L6-v2"] == 384
    assert _KNOWN_DIMENSIONS["all-mpnet-base-v2"] == 768
