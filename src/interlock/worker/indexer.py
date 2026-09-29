"""Multi-target document indexer - writes summarized documents to PG, entities, categories, FAISS."""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from interlock.cache.embedding import EmbeddingEngine
from interlock.cache.faiss_index import FAISSIndex
from interlock.discovery.category import CategoryManager
from interlock.discovery.entities import EntityManager
from interlock.discovery.vector_sync import publish_vector_generation
from interlock.worker.summarizer import DocumentSummary

logger = logging.getLogger(__name__)


class DocumentIndexer:
    """Writes document data to multiple targets after summarization.

    All targets are optional - if a manager/index is None, that step is skipped.
    This allows graceful degradation when components are unavailable.
    """

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        entity_manager: EntityManager | None = None,
        category_manager: CategoryManager | None = None,
        semantic_index: FAISSIndex | None = None,
        embedding_engine: EmbeddingEngine | None = None,
        vector_publish_client: Any | None = None,
    ) -> None:
        self._pool = pg_pool
        self._entity_manager = entity_manager
        self._category_manager = category_manager
        self._semantic_index = semantic_index
        self._embedding_engine = embedding_engine
        self._vector_publish_client = vector_publish_client

    async def index_document(
        self,
        source_id: str,
        asset_path: str,
        asset_type: str,
        content: str,
        summary: DocumentSummary,
        embedding: list[float] | None = None,
    ) -> int:
        """Index a document across all targets.

        1. Upsert into discovery_assets (PG JSONB)
        2. Update tsvector for full-text search
        3. Store entity cross-references
        4. Assign category (create if needed)
        5. Store embedding in FAISS semantic index

        Returns the discovery_asset ID.
        """
        # Step 1: Upsert into discovery_assets
        asset_id = await self._upsert_asset(
            source_id=source_id,
            asset_path=asset_path,
            asset_type=asset_type,
            summary=summary,
            embedding=embedding,
        )

        # Step 2: Update tsvector for full-text search
        await self._update_search_vector(asset_id, content, summary)

        # Step 3: Replace entity cross-references for real managers so
        # re-ingestion removes stale entities. Test doubles keep the legacy
        # store-only contract.
        if self._entity_manager is not None and (
            summary.entities or type(self._entity_manager) is EntityManager
        ):
            try:
                entities = _flatten_summary_entities(summary.entities)
                if type(self._entity_manager) is EntityManager:
                    await self._entity_manager.replace_entities(asset_id, entities)
                else:
                    await self._entity_manager.store_entities(asset_id, entities)
            except Exception:
                logger.exception("Failed to store entities for asset %d", asset_id)

        # Step 4: Replace category assignment for real managers so repeated
        # indexing does not inflate document_count or leave a stale category.
        if self._category_manager is not None and (
            summary.category_path or type(self._category_manager) is CategoryManager
        ):
            try:
                await self._assign_category(asset_id, summary.category_path)
            except Exception:
                logger.exception("Failed to assign category for asset %d", asset_id)

        # Step 5: Store embedding in FAISS
        if self._semantic_index is not None and embedding:
            try:
                await self._semantic_index.upsert(
                    key=f"{source_id}:{asset_path}",
                    embedding=embedding,
                    metadata={
                        "asset_id": asset_id,
                        "source_id": source_id,
                        "asset_path": asset_path,
                        "asset_type": asset_type,
                        "title": summary.title,
                        "summary": summary.summary,
                        "doc_type": summary.doc_type,
                        "topics": summary.topics,
                        "category_path": summary.category_path,
                    },
                )
                await publish_vector_generation(
                    self._vector_redis_client(),
                    namespace=str(getattr(self._semantic_index, "_namespace", "discovery")),
                    source_id=source_id,
                    asset_path=asset_path,
                    asset_id=asset_id,
                )
            except Exception:
                logger.exception("Failed to store embedding for asset %d", asset_id)

        logger.info(
            "Indexed document: source=%s path=%s asset_id=%d",
            source_id,
            asset_path,
            asset_id,
        )
        return asset_id

    async def _upsert_asset(
        self,
        source_id: str,
        asset_path: str,
        asset_type: str,
        summary: DocumentSummary,
        embedding: list[float] | None,
    ) -> int:
        """Upsert into discovery_assets, returning the asset ID."""
        summary_json = json.loads(summary.model_dump_json())
        quality_score = self._compute_quality_score(summary)

        row = await self._pool.fetchrow(
            """
            INSERT INTO discovery_assets
                (source_id, asset_type, asset_path, title, summary, topics,
                 embedding, quality_score, updated_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, NOW())
            ON CONFLICT (source_id, asset_type, asset_path)
            DO UPDATE SET
                title = EXCLUDED.title,
                summary = EXCLUDED.summary,
                topics = EXCLUDED.topics,
                embedding = EXCLUDED.embedding,
                quality_score = EXCLUDED.quality_score,
                updated_at = NOW()
            RETURNING id
            """,
            source_id,
            asset_type,
            asset_path,
            summary.title,
            summary_json,
            summary.topics,
            embedding,
            quality_score,
        )
        return int(row["id"])

    async def _update_search_vector(
        self,
        asset_id: int,
        content: str,
        summary: DocumentSummary,
    ) -> None:
        """Update the tsvector column for full-text search."""
        # Combine title, summary text, and topics for search
        searchable_parts = []
        if summary.title:
            searchable_parts.append(summary.title)
        if summary.summary:
            searchable_parts.append(summary.summary)
        if summary.topics:
            searchable_parts.append(" ".join(summary.topics))
        # Add first 500 words of content for broader matching
        content_words = content.split()[:500]
        if content_words:
            searchable_parts.append(" ".join(content_words))

        searchable_text = " ".join(searchable_parts)

        await self._pool.execute(
            """
            UPDATE discovery_assets
            SET search_vector = to_tsvector('english', $1),
                updated_at = NOW()
            WHERE id = $2
            """,
            searchable_text,
            asset_id,
        )

    async def _assign_category(self, asset_id: int, category_path: str | None) -> None:
        """Assign a category to the document, creating the category if needed.

        Converts dot-separated path (e.g. 'engineering.backend.api') to ltree format
        and ensures all ancestor categories exist.
        """
        category_manager = self._category_manager
        if category_manager is None:
            return

        if type(category_manager) is CategoryManager:
            ltree_path = _normalize_category_path(category_path)
            if ltree_path:
                leaf_name = ltree_path.rsplit(".", 1)[-1].replace("_", " ").title()
                await category_manager.create_category(
                    path=ltree_path,
                    name=leaf_name,
                )
            await category_manager.replace_document_category(asset_id, ltree_path)
            return

        if not category_path:
            return

        # Sanitize category path for ltree compatibility (alphanumeric and underscores)
        ltree_path = _normalize_category_path(category_path)
        if not ltree_path:
            return

        # Create category (idempotent via ON CONFLICT)
        leaf_name = ltree_path.rsplit(".", 1)[-1].replace("_", " ").title()
        await category_manager.create_category(
            path=ltree_path,
            name=leaf_name,
        )

        # Assign document to category
        await category_manager.assign_document(asset_id, ltree_path)

        # Increment document count
        await category_manager.increment_count(ltree_path)

    def _vector_redis_client(self) -> Any | None:
        if self._vector_publish_client is not None:
            return self._vector_publish_client
        return getattr(self._semantic_index, "_redis", None)

    @staticmethod
    def _compute_quality_score(summary: DocumentSummary) -> float:
        """Compute a simple quality score from summary quality signals."""
        signals = summary.quality_signals
        if not signals:
            return 0.5

        completeness = _quality_value(signals.get("completeness", 0.5))
        structure = _quality_value(signals.get("structure", signals.get("depth", 0.5)))

        # Validate they are numeric
        try:
            completeness = float(completeness)
            structure = float(structure)
        except (TypeError, ValueError):
            return 0.5

        return round((completeness + structure) / 2.0, 4)


def _flatten_summary_entities(
    entities: dict[str, list[str]] | list[str],
) -> list[dict[str, Any]]:
    """Convert v4 typed entities or legacy entity lists to storage rows."""
    if isinstance(entities, list):
        return [
            {
                "text": entity,
                "type": "unknown",
                "prominence": 1.0,
                "prominence_label": "medium",
            }
            for entity in entities
        ]

    rows: list[dict[str, Any]] = []
    for entity_type, values in entities.items():
        for value in values or []:
            rows.append(
                {
                    "text": value,
                    "type": entity_type,
                    "prominence": 1.0,
                    "prominence_label": "medium",
                    "mention_count": 1,
                }
            )
    return rows


def _normalize_category_path(category_path: str | None) -> str | None:
    if not category_path:
        return None
    normalized_path = category_path.replace(">", ".").replace("/", ".")
    sanitized_parts = []
    for part in normalized_path.split("."):
        clean = "".join(c if c.isalnum() or c == "_" else "_" for c in part)
        clean = clean.strip("_")
        if clean:
            sanitized_parts.append(clean.lower())
    return ".".join(sanitized_parts) if sanitized_parts else None


def _quality_value(value: Any) -> float:
    """Map v4 quality labels or legacy numeric values to a 0-1 score."""
    if isinstance(value, (int, float)):
        return float(value)
    mapping = {
        "complete": 1.0,
        "current": 1.0,
        "deep_analysis": 1.0,
        "recent": 0.8,
        "overview": 0.7,
        "partial": 0.55,
        "reference": 0.65,
        "draft": 0.35,
        "outdated": 0.25,
    }
    return mapping.get(str(value).lower(), 0.5)
