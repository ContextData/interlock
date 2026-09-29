"""Category classifier using embedding similarity.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import logging
import math

import asyncpg

from interlock.cache.embedding import EmbeddingEngine

logger = logging.getLogger(__name__)


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product of two vectors."""
    return sum(x * y for x, y in zip(a, b))


def _norm(vec: list[float]) -> float:
    """L2 norm of a vector."""
    return math.sqrt(sum(v * v for v in vec))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    na = _norm(a)
    nb = _norm(b)
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return _dot(a, b) / (na * nb)


class CategoryClassifier:
    """Classifies queries into categories to narrow search scope."""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        embedding_engine: EmbeddingEngine | None = None,
    ) -> None:
        self._pool = pg_pool
        self._engine = embedding_engine
        self._category_embeddings: dict[str, list[float]] = {}

    async def load_categories(self) -> None:
        """Load category embeddings from PG."""
        rows = await self._pool.fetch("""
            SELECT path::text AS path, embedding
            FROM category_taxonomy
            WHERE embedding IS NOT NULL
            """)
        self._category_embeddings.clear()
        for row in rows:
            embedding = row["embedding"]
            if embedding:
                self._category_embeddings[row["path"]] = list(embedding)

        logger.info("Loaded %d category embeddings", len(self._category_embeddings))

    async def classify(self, query: str, top_k: int = 3) -> list[tuple[str, float]]:
        """Classify a query into categories.

        Returns top_k (category_path, similarity_score) tuples.
        If embedding engine not available, return empty list.
        """
        if self._engine is None or not self._engine.available:
            return []

        if not self._category_embeddings:
            return []

        query_embedding = await self._engine.embed(query)
        if not query_embedding:
            return []

        scored: list[tuple[str, float]] = []
        for path, cat_embedding in self._category_embeddings.items():
            sim = _cosine_similarity(query_embedding, cat_embedding)
            scored.append((path, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
