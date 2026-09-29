"""Entity-document cross-reference manager for discovery."""

from __future__ import annotations

import logging
import re

import asyncpg

logger = logging.getLogger(__name__)


class EntityManager:
    """Manage entity-document cross-references in entity_document_xref."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self._pool = pg_pool

    async def store_entities(
        self,
        document_id: int,
        entities: list[dict],  # [{"text": "...", "type": "...", "prominence": 0.8}]
    ) -> int:
        """Store entity-document cross-references.

        Normalizes text (lowercase, strip). Uses INSERT ON CONFLICT DO UPDATE
        to update prominence when a higher value is provided.
        Returns count stored.
        """
        if not entities:
            return 0

        async with self._pool.acquire() as conn:
            count = await self._store_entities_on_conn(conn, document_id, entities)

        return count

    async def replace_entities(
        self,
        document_id: int,
        entities: list[dict],
    ) -> int:
        """Replace all entity-document cross-references for one document."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM entity_document_xref WHERE document_id = $1",
                document_id,
            )
            if not entities:
                return 0
            return await self._store_entities_on_conn(conn, document_id, entities)

    async def _store_entities_on_conn(
        self,
        conn,
        document_id: int,
        entities: list[dict],
    ) -> int:
        count = 0
        for entity in entities:
            text = self.normalize_entity(entity["text"])
            entity_type = entity.get("type", "unknown")
            prominence = entity.get("prominence", 1.0)
            prominence_label = entity.get("prominence_label")
            mention_count = int(entity.get("mention_count", 1) or 1)
            context_snippet = entity.get("context_snippet")

            if not text:
                continue

            await conn.execute(
                """
                INSERT INTO entity_document_xref
                    (entity_text, entity_type, document_id, prominence,
                     prominence_label, mention_count, context_snippet, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                ON CONFLICT (entity_text, entity_type, document_id)
                DO UPDATE SET
                    prominence = GREATEST(
                        entity_document_xref.prominence, EXCLUDED.prominence
                    ),
                    prominence_label = COALESCE(
                        EXCLUDED.prominence_label,
                        entity_document_xref.prominence_label
                    ),
                    mention_count = GREATEST(
                        entity_document_xref.mention_count,
                        EXCLUDED.mention_count
                    ),
                    context_snippet = COALESCE(
                        EXCLUDED.context_snippet,
                        entity_document_xref.context_snippet
                    ),
                    updated_at = NOW()
                """,
                text,
                entity_type,
                document_id,
                prominence,
                prominence_label,
                mention_count,
                context_snippet,
            )
            count += 1
        return count

    async def get_entities_for_document(self, document_id: int) -> list[dict]:
        """Get all entities for a document."""
        rows = await self._pool.fetch(
            """
            SELECT entity_text, entity_type, prominence, metadata
            FROM entity_document_xref
            WHERE document_id = $1
            ORDER BY prominence DESC
            """,
            document_id,
        )
        return [dict(r) for r in rows]

    async def get_documents_for_entity(
        self, entity_text: str, entity_type: str | None = None
    ) -> list[dict]:
        """Get all documents that mention an entity."""
        text = self.normalize_entity(entity_text)

        if entity_type is not None:
            rows = await self._pool.fetch(
                """
                SELECT x.document_id, x.entity_type, x.prominence,
                       d.source_id, d.asset_type, d.asset_path, d.title
                FROM entity_document_xref x
                JOIN discovery_assets d ON d.id = x.document_id
                WHERE x.entity_text = $1 AND x.entity_type = $2
                ORDER BY x.prominence DESC
                """,
                text,
                entity_type,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT x.document_id, x.entity_type, x.prominence,
                       d.source_id, d.asset_type, d.asset_path, d.title
                FROM entity_document_xref x
                JOIN discovery_assets d ON d.id = x.document_id
                WHERE x.entity_text = $1
                ORDER BY x.prominence DESC
                """,
                text,
            )
        return [dict(r) for r in rows]

    async def get_related_documents(self, document_id: int, limit: int = 10) -> list[dict]:
        """Find related documents via shared entities.

        Returns documents ranked by number of shared entities.
        """
        rows = await self._pool.fetch(
            """
            SELECT x2.document_id,
                   COUNT(*) AS shared_entities,
                   d.source_id, d.asset_type, d.asset_path, d.title
            FROM entity_document_xref x1
            JOIN entity_document_xref x2
                ON x1.entity_text = x2.entity_text
                AND x1.entity_type = x2.entity_type
                AND x2.document_id != $1
            JOIN discovery_assets d ON d.id = x2.document_id
            WHERE x1.document_id = $1
            GROUP BY x2.document_id, d.source_id, d.asset_type,
                     d.asset_path, d.title
            ORDER BY shared_entities DESC
            LIMIT $2
            """,
            document_id,
            limit,
        )
        return [dict(r) for r in rows]

    async def get_co_occurring_entities(self, entity_text: str, limit: int = 20) -> list[dict]:
        """Find entities that co-occur with the given entity across documents."""
        text = self.normalize_entity(entity_text)
        rows = await self._pool.fetch(
            """
            SELECT x2.entity_text, x2.entity_type,
                   COUNT(DISTINCT x2.document_id) AS co_occurrence_count
            FROM entity_document_xref x1
            JOIN entity_document_xref x2
                ON x1.document_id = x2.document_id
                AND (x2.entity_text != $1 OR x2.entity_type != x1.entity_type)
            WHERE x1.entity_text = $1
            GROUP BY x2.entity_text, x2.entity_type
            ORDER BY co_occurrence_count DESC
            LIMIT $2
            """,
            text,
            limit,
        )
        return [dict(r) for r in rows]

    @staticmethod
    def normalize_entity(text: str) -> str:
        """Normalize entity text: lowercase, strip whitespace, collapse spaces."""
        result = text.strip().lower()
        result = re.sub(r"\s+", " ", result)
        return result
