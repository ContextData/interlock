"""Unit tests for DiscoverySearch."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from interlock.discovery.search import DiscoverySearch
from interlock.models import SearchResult, SemanticMatch


def _make_result(asset_id: int, score: float, strategy: str) -> SearchResult:
    return SearchResult(
        asset_id=asset_id,
        source_id="src1",
        asset_type="document",
        asset_path=f"/doc/{asset_id}",
        title=f"Doc {asset_id}",
        score=score,
        strategy=strategy,
    )


def _rrf(rank: int) -> float:
    return 1.0 / (DiscoverySearch.RRF_K + rank)


# ---------------------------------------------------------------------------
# _merge_results
# ---------------------------------------------------------------------------


class TestMergeResults:
    def _make_search(self):
        pool = AsyncMock()
        return DiscoverySearch(pg_pool=pool)

    def test_deduplicates_by_asset_id(self):
        search = self._make_search()
        list1 = [_make_result(1, 3.0, "vector")]
        list2 = [_make_result(1, 2.0, "fulltext")]

        merged = search._merge_results(list1, list2, limit=10)
        assert len(merged) == 1
        assert merged[0].asset_id == 1
        assert merged[0].score == pytest.approx(_rrf(1) + _rrf(1))
        assert merged[0].metadata["ranker"] == "rrf"
        assert merged[0].metadata["matched_strategies"] == ["fulltext", "vector"]
        assert merged[0].metadata["strategy_ranks"] == {"vector": 1, "fulltext": 1}
        assert merged[0].metadata["strategy_scores"] == {"vector": 3.0, "fulltext": 2.0}

    def test_sorts_by_rrf_score_descending(self):
        search = self._make_search()
        list1 = [_make_result(1, 100.0, "vector"), _make_result(2, 1.0, "vector")]

        merged = search._merge_results(list1, limit=10)
        assert [result.asset_id for result in merged] == [1, 2]
        assert merged[0].score == pytest.approx(_rrf(1))
        assert merged[1].score == pytest.approx(_rrf(2))

    def test_rrf_rewards_results_seen_across_strategies(self):
        search = self._make_search()
        vec = [_make_result(1, 0.99, "vector"), _make_result(2, 0.98, "vector")]
        ft = [_make_result(2, 0.10, "fulltext")]

        merged = search._merge_results(vec, ft, limit=10)

        assert [result.asset_id for result in merged] == [2, 1]
        assert merged[0].score == pytest.approx(_rrf(2) + _rrf(1))
        assert merged[0].metadata["best_rank"] == 1

    def test_rrf_ties_break_by_best_rank_then_asset_id(self):
        search = self._make_search()
        vec = [_make_result(2, 10.0, "vector"), _make_result(1, 10.0, "vector")]
        ft = [_make_result(3, 10.0, "fulltext")]

        merged = search._merge_results(vec, ft, limit=10)

        assert [result.asset_id for result in merged] == [2, 3, 1]

    def test_respects_limit(self):
        search = self._make_search()
        results = [_make_result(i, float(i), "vector") for i in range(20)]

        merged = search._merge_results(results, limit=5)
        assert len(merged) == 5

    def test_empty_lists(self):
        search = self._make_search()
        merged = search._merge_results([], [], [], limit=10)
        assert merged == []

    def test_multiple_strategies_merge(self):
        search = self._make_search()
        vec = [_make_result(1, 3.0, "vector"), _make_result(2, 2.0, "vector")]
        ft = [_make_result(2, 1.5, "fulltext"), _make_result(3, 1.0, "fulltext")]
        meta = [_make_result(3, 0.5, "metadata")]

        merged = search._merge_results(vec, ft, meta, limit=10)
        scores = {r.asset_id: r.score for r in merged}
        assert scores[1] == pytest.approx(_rrf(1))
        assert scores[2] == pytest.approx(_rrf(2) + _rrf(1))
        assert scores[3] == pytest.approx(_rrf(2) + _rrf(1))


# ---------------------------------------------------------------------------
# _vector_search
# ---------------------------------------------------------------------------


class TestVectorSearch:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_engine(self):
        pool = AsyncMock()
        search = DiscoverySearch(pg_pool=pool, semantic_index=None, embedding_engine=None)
        result = await search._vector_search("test", 10)
        assert result == []

    @pytest.mark.asyncio
    async def test_searches_faiss(self):
        pool = AsyncMock()

        engine = MagicMock()
        type(engine).available = PropertyMock(return_value=True)
        engine.embed = AsyncMock(return_value=[0.1] * 384)

        index = MagicMock()
        type(index).available = PropertyMock(return_value=True)
        index.search = AsyncMock(
            return_value=[
                SemanticMatch(
                    key="k1",
                    score=0.95,
                    metadata={
                        "asset_id": 1,
                        "source_id": "src1",
                        "asset_type": "doc",
                        "asset_path": "/a",
                    },
                )
            ]
        )

        search = DiscoverySearch(pg_pool=pool, semantic_index=index, embedding_engine=engine)
        results = await search._vector_search("test query", 10)

        assert len(results) == 1
        assert results[0].asset_id == 1
        assert results[0].strategy == "vector"
        assert results[0].score == 0.95


# ---------------------------------------------------------------------------
# _fulltext_search
# ---------------------------------------------------------------------------


class TestFulltextSearch:
    @pytest.mark.asyncio
    async def test_queries_pg(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "id": 5,
                    "source_id": "src1",
                    "asset_type": "document",
                    "asset_path": "/doc/5",
                    "title": "Test Doc",
                    "summary": "A summary",
                    "rank": 0.8,
                }
            ]
        )

        search = DiscoverySearch(pg_pool=pool)
        results = await search._fulltext_search("test", 10)

        assert len(results) == 1
        assert results[0].asset_id == 5
        assert results[0].strategy == "fulltext"
        assert results[0].score == 0.8
        sql = pool.fetch.call_args.args[0]
        assert "ORDER BY rank DESC, id ASC" in sql
        pool.fetch.assert_called_once()


# ---------------------------------------------------------------------------
# _metadata_search
# ---------------------------------------------------------------------------


class TestMetadataSearch:
    @pytest.mark.asyncio
    async def test_queries_pg(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "source_id": "src1",
                    "asset_type": "table",
                    "asset_path": "/tbl/7",
                    "title": "My Table",
                    "summary": None,
                    "topics": ["python"],
                }
            ]
        )

        search = DiscoverySearch(pg_pool=pool)
        results = await search._metadata_search("python", 10)

        assert len(results) == 1
        assert results[0].asset_id == 7
        assert results[0].strategy == "metadata"
        assert results[0].score == 1.0
        sql = pool.fetch.call_args.args[0]
        assert "ORDER BY title ASC NULLS LAST, id ASC" in sql


# ---------------------------------------------------------------------------
# search (integration - parallel execution)
# ---------------------------------------------------------------------------


class TestSearchParallel:
    @pytest.mark.asyncio
    async def test_runs_all_three_strategies(self):
        pool = AsyncMock()
        search = DiscoverySearch(pg_pool=pool)

        vec_result = [_make_result(1, 3.0, "vector")]
        ft_result = [_make_result(2, 2.0, "fulltext")]
        meta_result = [_make_result(3, 1.0, "metadata")]

        with (
            patch.object(
                search, "_vector_search", new_callable=AsyncMock, return_value=vec_result
            ) as mock_vec,
            patch.object(
                search, "_fulltext_search", new_callable=AsyncMock, return_value=ft_result
            ) as mock_ft,
            patch.object(
                search, "_metadata_search", new_callable=AsyncMock, return_value=meta_result
            ) as mock_meta,
        ):
            results = await search.search("test query", limit=10)

            mock_vec.assert_called_once()
            mock_ft.assert_called_once()
            mock_meta.assert_called_once()

            assert len(results) == 3
            # Top-ranked result from the first strategy wins when each appears once.
            assert results[0].asset_id == 1

    @pytest.mark.asyncio
    async def test_handles_strategy_failure(self):
        pool = AsyncMock()
        search = DiscoverySearch(pg_pool=pool)

        ft_result = [_make_result(2, 2.0, "fulltext")]

        with (
            patch.object(
                search, "_vector_search", new_callable=AsyncMock, side_effect=RuntimeError("fail")
            ),
            patch.object(
                search, "_fulltext_search", new_callable=AsyncMock, return_value=ft_result
            ),
            patch.object(search, "_metadata_search", new_callable=AsyncMock, return_value=[]),
        ):
            results = await search.search("test query", limit=10)
            assert len(results) == 1
            assert results[0].asset_id == 2
