"""Embedding engine for semantic cache lookups.

Wraps sentence-transformers to produce normalized text embeddings.
Supports configurable model selection and optional ONNX Runtime
optimization.

Gracefully degrades when sentence-transformers/torch are not installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# Lazy import guard - resolved at initialize() time
_sentence_transformers = None

# Known model dimensions - used as fallback before model is loaded
_KNOWN_DIMENSIONS: dict[str, int] = {
    "all-MiniLM-L6-v2": 384,
    "all-mpnet-base-v2": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "all-MiniLM-L12-v2": 384,
    "deterministic-test": 384,
}

# Default model and dimension
DEFAULT_MODEL = "all-MiniLM-L6-v2"
DEFAULT_DIMENSION = 384


class EmbeddingEngine:
    """Manages a sentence-transformers model for text embedding.

    Supports multiple models with dynamic dimension detection:
      - all-MiniLM-L6-v2 (384-dim, default, fast)
      - all-mpnet-base-v2 (768-dim, higher quality)
      - BAAI/bge-small-en-v1.5 (384-dim, better quality than MiniLM)

    Optional ONNX Runtime backend for faster inference when use_onnx=True
    and optimum[onnxruntime] is installed.

    Gracefully degrades if sentence-transformers is not installed.
    Uses run_in_executor to avoid blocking the event loop.
    """

    def __init__(
        self,
        model_name: str | None = None,
        use_onnx: bool = False,
    ) -> None:
        self._model_name = model_name or os.environ.get("INTERLOCK_EMBEDDING_MODEL", DEFAULT_MODEL)
        self._use_onnx = use_onnx
        self._model: Any = None
        self._available = False
        self._deterministic = self._model_name in {"deterministic-test", "deterministic"}
        self._dimension: int = _KNOWN_DIMENSIONS.get(self._model_name, DEFAULT_DIMENSION)
        self._executor: ThreadPoolExecutor | None = None

    async def initialize(self) -> None:
        """Load the model.

        If sentence-transformers is not installed, logs a warning and
        sets _available=False instead of raising.

        When use_onnx=True, attempts to load the ONNX-optimized backend
        via the optimum library. Falls back to the standard PyTorch
        backend if ONNX is unavailable.
        """
        if self._deterministic:
            self._available = True
            logger.info(
                "Embedding engine initialized in deterministic test mode: dim=%d",
                self._dimension,
            )
            return

        global _sentence_transformers  # noqa: PLW0603
        try:
            import sentence_transformers as st

            _sentence_transformers = st
        except ImportError:
            logger.warning(
                "sentence-transformers is not installed - "
                "embedding engine will be unavailable. "
                "Install with: pip install sentence-transformers"
            )
            self._available = False
            return

        self._executor = ThreadPoolExecutor(max_workers=1)

        loop = asyncio.get_running_loop()
        try:
            if self._use_onnx:
                self._model = await loop.run_in_executor(
                    self._executor,
                    self._load_onnx_model,
                )
            else:
                self._model = await loop.run_in_executor(
                    self._executor,
                    lambda: _sentence_transformers.SentenceTransformer(self._model_name),
                )

            # Detect actual dimension from the loaded model
            self._dimension = self._detect_dimension()

            self._available = True
            logger.info(
                "Embedding engine initialized: model=%s, dim=%d, onnx=%s",
                self._model_name,
                self._dimension,
                self._use_onnx and self._model is not None,
            )
        except Exception:
            logger.exception("Failed to load embedding model %s", self._model_name)
            self._available = False

    def _load_onnx_model(self) -> Any:
        """Attempt to load model with ONNX Runtime backend.

        Falls back to standard PyTorch if optimum is not installed.
        """
        try:
            from optimum.onnxruntime import ORTModelForFeatureExtraction  # noqa: F401

            # sentence-transformers >= 2.3.0 supports backend parameter
            model = _sentence_transformers.SentenceTransformer(
                self._model_name,
                backend="onnx",
            )
            logger.info("Loaded ONNX backend for model %s", self._model_name)
            return model
        except (ImportError, TypeError, Exception) as exc:
            logger.warning(
                "ONNX backend unavailable for %s (%s), falling back to PyTorch",
                self._model_name,
                exc,
            )
            return _sentence_transformers.SentenceTransformer(self._model_name)

    def _detect_dimension(self) -> int:
        """Detect embedding dimension from the loaded model."""
        if self._model is None:
            return _KNOWN_DIMENSIONS.get(self._model_name, DEFAULT_DIMENSION)

        # sentence-transformers exposes dimension via get_sentence_embedding_dimension
        try:
            dim = self._model.get_sentence_embedding_dimension()
            if dim is not None:
                return int(dim)
        except (AttributeError, TypeError):
            pass

        # Fallback to known dimensions table
        return _KNOWN_DIMENSIONS.get(self._model_name, DEFAULT_DIMENSION)

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string.

        Returns a float vector whose dimension depends on the loaded model.
        If the engine is not available, returns an empty list.
        Uses run_in_executor to avoid blocking the event loop.
        """
        if self._deterministic:
            return _deterministic_vector(text, self._dimension)

        if not self._available or self._model is None:
            return []

        loop = asyncio.get_running_loop()
        vector = await loop.run_in_executor(
            self._executor,
            lambda: self._model.encode(text, normalize_embeddings=True),
        )
        return vector.tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts. More efficient than calling embed() in a loop."""
        if self._deterministic:
            return [_deterministic_vector(text, self._dimension) for text in texts]

        if not self._available or self._model is None:
            return [[] for _ in texts]

        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(
            self._executor,
            lambda: self._model.encode(texts, normalize_embeddings=True, batch_size=32),
        )
        return vectors.tolist()

    async def shutdown(self) -> None:
        """Shutdown the thread pool executor."""
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None
        self._model = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dimension(self) -> int:
        """Return embedding dimension, detected from the loaded model.

        Before initialization, returns the dimension from the known
        models table (384 for the default model).
        """
        return self._dimension

    @property
    def model_info(self) -> dict[str, Any]:
        """Return metadata about the current model configuration."""
        return {
            "model_name": self._model_name,
            "dimension": self._dimension,
            "use_onnx": self._use_onnx,
            "available": self._available,
        }


def _deterministic_vector(text: str, dimension: int) -> list[float]:
    """Return a stable normalized vector for compose-backed tests.

    This is intentionally not semantically meaningful. It gives E2E tests a
    reproducible vector path without downloading external model weights.
    """
    values: list[float] = []
    seed = text.encode("utf-8", errors="ignore")
    for idx in range(dimension):
        digest = hashlib.blake2b(
            seed + idx.to_bytes(4, "big"),
            digest_size=8,
        ).digest()
        integer = int.from_bytes(digest, "big")
        values.append((integer / ((1 << 64) - 1)) * 2.0 - 1.0)

    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]
