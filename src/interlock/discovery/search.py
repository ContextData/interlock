"""Parallel retrieval engine combining vector, full-text, and metadata search."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg

from interlock.cache.embedding import EmbeddingEngine
from interlock.cache.faiss_index import FAISSIndex
from interlock.discovery.vector_sync import reload_index_if_generation_changed
from interlock.models import SearchResult

logger = logging.getLogger(__name__)


class DiscoverySearch:
    """Runs three search strategies in parallel and merges results."""

    # Standard RRF constant. Larger values dampen rank contribution so a
    # result that appears across strategies beats a one-off top result.
    RRF_K = 60

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        semantic_index: FAISSIndex | None = None,
        embedding_engine: EmbeddingEngine | None = None,
    ) -> None:
        self._pool = pg_pool
        self._index = semantic_index
        self._engine = embedding_engine

    async def search(
        self,
        query: str,
        source_id: str | None = None,
        category_path: str | None = None,
        metadata_filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Execute parallel retrieval and merge results.

        Three strategies run concurrently:
        1. Vector search via FAISS
        2. Full-text search via PG tsvector
        3. Metadata search via PG WHERE on topics/title

        Results are deduplicated by asset_id and fused with reciprocal rank
        fusion so incomparable vector, full-text, and metadata scores are not
        summed directly.
        """
        tasks = [
            self._vector_search(query, limit, source_id, category_path),
            self._fulltext_search(query, limit, source_id, category_path),
            self._metadata_search(query, limit, source_id, category_path, metadata_filters),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_results: list[list[SearchResult]] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                strategy = ["vector", "fulltext", "metadata"][i]
                logger.warning("Search strategy %s failed: %s", strategy, result)
                all_results.append([])
            else:
                all_results.append(result)

        return self._merge_results(*all_results, limit=limit)

    async def _vector_search(
        self,
        query: str,
        limit: int,
        source_id: str | None = None,
        category_path: str | None = None,
    ) -> list[SearchResult]:
        """Search via FAISS semantic index."""
        if self._engine is None or self._index is None:
            return []

        if not self._engine.available or not self._index.available:
            return []

        embedding = await self._engine.embed(query)
        if not embedding:
            return []

        await reload_index_if_generation_changed(self._index, namespace="discovery")

        filters: dict[str, Any] = {}
        if source_id:
            filters["source_id"] = source_id

        matches = await self._index.search(embedding, top_k=limit, filters=filters)

        results: list[SearchResult] = []
        for match in matches:
            meta = match.metadata
            result = SearchResult(
                asset_id=meta.get("asset_id", 0),
                source_id=meta.get("source_id", ""),
                asset_type=meta.get("asset_type", ""),
                asset_path=meta.get("asset_path", ""),
                title=meta.get("title"),
                summary=meta.get("summary"),
                score=match.score,
                strategy="vector",
                metadata=meta,
            )
            results.append(result)

        return results

    async def _fulltext_search(
        self,
        query: str,
        limit: int,
        source_id: str | None = None,
        category_path: str | None = None,
    ) -> list[SearchResult]:
        """Search via PostgreSQL tsvector full-text search."""
        conditions = ["search_vector @@ plainto_tsquery('english', $1)"]
        params: list[Any] = [query]
        idx = 2

        if source_id:
            conditions.append(f"source_id = ${idx}")
            params.append(source_id)
            idx += 1

        if category_path:
            conditions.append(f"category_path <@ ${idx}::ltree")
            params.append(category_path)
            idx += 1

        where = " AND ".join(conditions)
        sql = f"""
            SELECT id, source_id, asset_type, asset_path, title,
                   summary::text AS summary,
                   ts_rank(search_vector, plainto_tsquery('english', $1)) AS rank
            FROM discovery_assets
            WHERE {where}
            ORDER BY rank DESC, id ASC
            LIMIT ${idx}
        """
        params.append(limit)

        rows = await self._pool.fetch(sql, *params)

        results: list[SearchResult] = []
        for row in rows:
            results.append(
                SearchResult(
                    asset_id=row["id"],
                    source_id=row["source_id"],
                    asset_type=row["asset_type"],
                    asset_path=row["asset_path"],
                    title=row["title"],
                    summary=row["summary"],
                    score=float(row["rank"]),
                    strategy="fulltext",
                )
            )

        return results

    async def _metadata_search(
        self,
        query: str,
        limit: int,
        source_id: str | None = None,
        category_path: str | None = None,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search via metadata: title ILIKE and topics array overlap."""
        conditions = ["(title ILIKE $1 OR $2 = ANY(topics))"]
        like_pattern = f"%{query}%"
        params: list[Any] = [like_pattern, query.lower()]
        idx = 3
        metadata_filters = metadata_filters or {}

        if source_id:
            conditions.append(f"source_id = ${idx}")
            params.append(source_id)
            idx += 1

        if category_path:
            conditions.append(f"category_path <@ ${idx}::ltree")
            params.append(category_path)
            idx += 1

        source_types = metadata_filters.get("source_types")
        if source_types:
            conditions.append(
                f"source_id IN (SELECT source_id FROM data_sources WHERE source_type = ANY(${idx}::text[]))"
            )
            params.append(source_types)
            idx += 1

        doc_types = metadata_filters.get("doc_types")
        if doc_types:
            conditions.append(f"summary->>'doc_type' = ANY(${idx}::text[])")
            params.append(doc_types)
            idx += 1

        audience = metadata_filters.get("audience")
        if audience:
            conditions.append(f"summary->>'audience' = ${idx}")
            params.append(audience)
            idx += 1

        entities = metadata_filters.get("entities")
        if entities:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM entity_document_xref ex "
                "WHERE ex.document_id = discovery_assets.id "
                f"AND ex.entity_text = ANY(${idx}::text[])"
                ")"
            )
            params.append([str(e).lower().strip() for e in entities])
            idx += 1

        where = " AND ".join(conditions)
        sql = f"""
            SELECT id, source_id, asset_type, asset_path, title,
                   summary::text AS summary, topics
            FROM discovery_assets
            WHERE {where}
            ORDER BY title ASC NULLS LAST, id ASC
            LIMIT ${idx}
        """
        params.append(limit)

        rows = await self._pool.fetch(sql, *params)

        results: list[SearchResult] = []
        for row in rows:
            results.append(
                SearchResult(
                    asset_id=row["id"],
                    source_id=row["source_id"],
                    asset_type=row["asset_type"],
                    asset_path=row["asset_path"],
                    title=row["title"],
                    summary=row["summary"],
                    score=1.0,
                    strategy="metadata",
                )
            )

        return results

    def _merge_results(
        self, *result_lists: list[SearchResult], limit: int = 20
    ) -> list[SearchResult]:
        """Merge and deduplicate results with reciprocal rank fusion."""
        merged: dict[int, SearchResult] = {}
        rrf_scores: dict[int, float] = {}
        best_ranks: dict[int, int] = {}
        strategy_ranks: dict[int, dict[str, int]] = {}
        strategy_scores: dict[int, dict[str, float]] = {}

        for results in result_lists:
            for rank, result in enumerate(results, start=1):
                aid = result.asset_id
                rrf_scores[aid] = rrf_scores.get(aid, 0.0) + 1.0 / (self.RRF_K + rank)
                best_ranks[aid] = min(best_ranks.get(aid, rank), rank)
                strategy_ranks.setdefault(aid, {})[result.strategy] = rank
                strategy_scores.setdefault(aid, {})[result.strategy] = result.score
                if aid in merged:
                    existing = merged[aid]
                    metadata = {
                        **existing.metadata,
                        "matched_strategies": sorted(
                            set(
                                existing.metadata.get(
                                    "matched_strategies",
                                    existing.strategy.split(","),
                                )
                            )
                            | {result.strategy}
                        ),
                    }
                    if not metadata.get("asset_path") and result.asset_path:
                        metadata["asset_path"] = result.asset_path
                    merged[aid] = existing.model_copy(
                        update={
                            "asset_path": existing.asset_path or result.asset_path,
                            "title": existing.title or result.title,
                            "summary": existing.summary or result.summary,
                            "strategy": f"{existing.strategy},{result.strategy}",
                            "metadata": metadata,
                        }
                    )
                else:
                    merged[aid] = result.model_copy(
                        update={
                            "metadata": {
                                **result.metadata,
                                "matched_strategies": [result.strategy],
                            }
                        }
                    )

        fused: list[SearchResult] = []
        for aid, result in merged.items():
            metadata = {
                **result.metadata,
                "ranker": "rrf",
                "rrf_k": self.RRF_K,
                "rrf_score": rrf_scores[aid],
                "best_rank": best_ranks[aid],
                "strategy_ranks": strategy_ranks.get(aid, {}),
                "strategy_scores": strategy_scores.get(aid, {}),
            }
            fused.append(result.model_copy(update={"score": rrf_scores[aid], "metadata": metadata}))

        sorted_results = sorted(
            fused,
            key=lambda r: (
                -r.score,
                r.metadata.get("best_rank", 10**9),
                r.asset_id,
            ),
        )
        return sorted_results[:limit]
