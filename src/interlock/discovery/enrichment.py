"""Entity enrichment for discovery search results.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import logging
import re

from interlock.discovery.entities import EntityManager
from interlock.models import SearchResult

logger = logging.getLogger(__name__)

# Boost factor for results that share entities with the query
ENTITY_BOOST = 1.5


class EntityEnricher:
    """Enriches discovery results with entity data."""

    def __init__(self, entity_manager: EntityManager) -> None:
        self._entities = entity_manager

    async def enrich_results(
        self,
        results: list[SearchResult],
        query_entities: list[str] | None = None,
    ) -> list[SearchResult]:
        """Add entity metadata to search results.

        For each result, fetches matching entities from the entity manager
        and attaches them to the result metadata. If query_entities is
        provided, results that share entities with the query get a score
        boost.
        """
        if not results:
            return results

        query_entity_set = {EntityManager.normalize_entity(e) for e in (query_entities or [])}

        enriched: list[SearchResult] = []
        for result in results:
            doc_entities = await self._entities.get_entities_for_document(result.asset_id)

            new_metadata = dict(result.metadata)
            new_metadata["entities"] = doc_entities

            score = result.score
            if query_entity_set and doc_entities:
                doc_entity_texts = {e["entity_text"] for e in doc_entities}
                shared = query_entity_set & doc_entity_texts
                if shared:
                    score = score * ENTITY_BOOST
                    new_metadata["shared_entities"] = list(shared)

            enriched.append(result.model_copy(update={"metadata": new_metadata, "score": score}))

        return enriched

    async def expand_query(self, query: str) -> list[str]:
        """Extract entities from query and find co-occurring entities
        for query expansion.

        Uses simple whitespace-based token extraction, then looks up
        each token as a potential entity to find co-occurring terms.
        Returns additional search terms (entity texts that co-occur
        with entities found in the query).
        """
        # Extract candidate entity tokens from the query
        tokens = _extract_tokens(query)
        if not tokens:
            return []

        expansion_terms: list[str] = []
        seen: set[str] = set()

        for token in tokens:
            co_occurring = await self._entities.get_co_occurring_entities(token, limit=5)
            for entity in co_occurring:
                text = entity["entity_text"]
                if text not in seen and text not in tokens:
                    seen.add(text)
                    expansion_terms.append(text)

        return expansion_terms


def _extract_tokens(query: str) -> list[str]:
    """Extract normalized tokens from a query string.

    Splits on whitespace, lowercases, strips punctuation, and removes
    very short tokens (length < 3).
    """
    raw = re.split(r"\s+", query.strip())
    tokens: list[str] = []
    for word in raw:
        cleaned = re.sub(r"[^\w]", "", word).lower()
        if len(cleaned) >= 3:
            tokens.append(cleaned)
    return tokens
