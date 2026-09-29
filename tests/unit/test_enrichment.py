"""Unit tests for EntityEnricher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.discovery.enrichment import ENTITY_BOOST, EntityEnricher
from interlock.models import SearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(asset_id: int = 1, score: float = 1.0, source_id: str = "src1") -> SearchResult:
    return SearchResult(
        asset_id=asset_id,
        source_id=source_id,
        asset_type="document",
        asset_path=f"/doc/{asset_id}",
        title=f"Doc {asset_id}",
        score=score,
        strategy="fulltext",
    )


def _mock_entity_manager(
    doc_entities: dict[int, list[dict]] | None = None,
    co_occurring: list[dict] | None = None,
) -> MagicMock:
    """Create a mock EntityManager."""
    em = MagicMock()

    async def get_entities(doc_id):
        if doc_entities is None:
            return []
        return doc_entities.get(doc_id, [])

    em.get_entities_for_document = AsyncMock(side_effect=get_entities)

    async def get_co(text, limit=20):
        return co_occurring or []

    em.get_co_occurring_entities = AsyncMock(side_effect=get_co)

    return em


# ---------------------------------------------------------------------------
# Tests: enrich_results
# ---------------------------------------------------------------------------


class TestEnrichResults:
    @pytest.mark.anyio
    async def test_adds_entities_to_metadata(self):
        doc_entities = {
            1: [
                {"entity_text": "python", "entity_type": "tech", "prominence": 0.9},
            ],
        }
        em = _mock_entity_manager(doc_entities=doc_entities)
        enricher = EntityEnricher(em)

        results = [_make_result(asset_id=1, score=2.0)]
        enriched = await enricher.enrich_results(results)

        assert len(enriched) == 1
        assert "entities" in enriched[0].metadata
        assert len(enriched[0].metadata["entities"]) == 1
        assert enriched[0].metadata["entities"][0]["entity_text"] == "python"

    @pytest.mark.anyio
    async def test_boosts_score_for_shared_entities(self):
        doc_entities = {
            1: [
                {"entity_text": "python", "entity_type": "tech", "prominence": 0.9},
                {"entity_text": "fastapi", "entity_type": "tech", "prominence": 0.7},
            ],
            2: [
                {"entity_text": "java", "entity_type": "tech", "prominence": 0.8},
            ],
        }
        em = _mock_entity_manager(doc_entities=doc_entities)
        enricher = EntityEnricher(em)

        results = [
            _make_result(asset_id=1, score=2.0),
            _make_result(asset_id=2, score=2.0),
        ]
        enriched = await enricher.enrich_results(results, query_entities=["Python"])

        # Doc 1 shares "python" with query -> boosted
        assert enriched[0].score == 2.0 * ENTITY_BOOST
        assert "shared_entities" in enriched[0].metadata
        assert "python" in enriched[0].metadata["shared_entities"]

        # Doc 2 does not share entities -> not boosted
        assert enriched[1].score == 2.0

    @pytest.mark.anyio
    async def test_empty_results_returns_empty(self):
        em = _mock_entity_manager()
        enricher = EntityEnricher(em)
        enriched = await enricher.enrich_results([])
        assert enriched == []

    @pytest.mark.anyio
    async def test_no_query_entities_no_boost(self):
        doc_entities = {
            1: [{"entity_text": "python", "entity_type": "tech", "prominence": 0.9}],
        }
        em = _mock_entity_manager(doc_entities=doc_entities)
        enricher = EntityEnricher(em)

        results = [_make_result(asset_id=1, score=3.0)]
        enriched = await enricher.enrich_results(results)

        # No query_entities means no boost
        assert enriched[0].score == 3.0
        assert "entities" in enriched[0].metadata


# ---------------------------------------------------------------------------
# Tests: expand_query
# ---------------------------------------------------------------------------


class TestExpandQuery:
    @pytest.mark.anyio
    async def test_returns_co_occurring_entities(self):
        co_occurring = [
            {"entity_text": "django", "entity_type": "tech", "co_occurrence_count": 5},
            {"entity_text": "flask", "entity_type": "tech", "co_occurrence_count": 3},
        ]
        em = _mock_entity_manager(co_occurring=co_occurring)
        enricher = EntityEnricher(em)

        terms = await enricher.expand_query("python web framework")

        assert "django" in terms
        assert "flask" in terms

    @pytest.mark.anyio
    async def test_empty_query_returns_empty(self):
        em = _mock_entity_manager()
        enricher = EntityEnricher(em)
        terms = await enricher.expand_query("")
        assert terms == []

    @pytest.mark.anyio
    async def test_deduplicates_expansion_terms(self):
        co_occurring = [
            {"entity_text": "react", "entity_type": "tech", "co_occurrence_count": 5},
        ]
        em = _mock_entity_manager(co_occurring=co_occurring)
        enricher = EntityEnricher(em)

        # Two tokens, both returning "react"
        terms = await enricher.expand_query("javascript typescript")

        # Should only appear once
        assert terms.count("react") == 1
