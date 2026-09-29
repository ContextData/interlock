"""PG ltree-based hierarchical category taxonomy for document classification."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class CategoryManager:
    """Manage hierarchical categories stored as ltree paths in category_taxonomy."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self._pool = pg_pool

    async def create_category(
        self,
        path: str,
        name: str,
        description: str | None = None,
        embedding: list[float] | None = None,
    ) -> int:
        """Create a category with ltree path. E.g. path='engineering.backend.api'.

        Uses INSERT ON CONFLICT DO NOTHING for idempotent creation.
        Returns the category ID.
        """
        row = await self._pool.fetchrow(
            """
            INSERT INTO category_taxonomy (path, name, description, embedding)
            VALUES ($1::ltree, $2, $3, $4)
            ON CONFLICT (path) DO NOTHING
            RETURNING id
            """,
            path,
            name,
            description,
            embedding,
        )
        if row is not None:
            return int(row["id"])
        # Already existed - fetch the existing ID
        existing = await self._pool.fetchrow(
            "SELECT id FROM category_taxonomy WHERE path = $1::ltree",
            path,
        )
        if existing is None:
            raise RuntimeError(f"Category at path {path!r} not found after upsert")
        return int(existing["id"])

    async def get_category(self, path: str) -> dict[str, Any] | None:
        """Get a single category by path."""
        row = await self._pool.fetchrow(
            """
            SELECT id, path::text AS path, name, description, embedding,
                   document_count, created_at
            FROM category_taxonomy
            WHERE path = $1::ltree
            """,
            path,
        )
        return dict(row) if row is not None else None

    async def get_children(self, parent_path: str) -> list[dict[str, Any]]:
        """Get direct children of a category.

        Direct children have exactly one more label than the parent, e.g.
        parent='a.b' -> children match 'a.b.*{1}'.
        """
        rows = await self._pool.fetch(
            """
            SELECT id, path::text AS path, name, description, embedding,
                   document_count, created_at
            FROM category_taxonomy
            WHERE path ~ ($1 || '.*{1}')::lquery
            ORDER BY path
            """,
            parent_path,
        )
        return [dict(r) for r in rows]

    async def get_subtree(self, root_path: str) -> list[dict[str, Any]]:
        """Get all descendants of a category (using ltree <@ operator).

        Includes the root itself.
        """
        rows = await self._pool.fetch(
            """
            SELECT id, path::text AS path, name, description, embedding,
                   document_count, created_at
            FROM category_taxonomy
            WHERE path <@ $1::ltree
            ORDER BY path
            """,
            root_path,
        )
        return [dict(r) for r in rows]

    async def get_roots(self) -> list[dict[str, Any]]:
        """Get top-level categories (single-segment paths with nlevel = 1)."""
        rows = await self._pool.fetch("""
            SELECT id, path::text AS path, name, description, embedding,
                   document_count, created_at
            FROM category_taxonomy
            WHERE nlevel(path) = 1
            ORDER BY path
            """)
        return [dict(r) for r in rows]

    async def assign_document(self, document_id: int, category_path: str) -> None:
        """Assign a document (discovery_assets) to a category by setting its category_path."""
        await self._pool.execute(
            """
            UPDATE discovery_assets
            SET category_path = $1::ltree, updated_at = NOW()
            WHERE id = $2
            """,
            category_path,
            document_id,
        )

    async def replace_document_category(
        self,
        document_id: int,
        category_path: str | None,
    ) -> bool:
        """Replace one document's category and repair affected document counts."""
        existing = await self._pool.fetchrow(
            """
            SELECT category_path::text AS category_path
            FROM discovery_assets
            WHERE id = $1
            """,
            document_id,
        )
        old_path = (
            str(existing["category_path"]) if existing and existing["category_path"] else None
        )

        if category_path:
            await self._pool.execute(
                """
                UPDATE discovery_assets
                SET category_path = $1::ltree, updated_at = NOW()
                WHERE id = $2
                """,
                category_path,
                document_id,
            )
        else:
            await self._pool.execute(
                """
                UPDATE discovery_assets
                SET category_path = NULL, updated_at = NOW()
                WHERE id = $1
                """,
                document_id,
            )

        paths_to_recount = {path for path in (old_path, category_path) if path}
        for path in paths_to_recount:
            await self.recount_category(path)
        return old_path != category_path

    async def increment_count(self, path: str) -> None:
        """Increment document_count for a category."""
        await self._pool.execute(
            """
            UPDATE category_taxonomy
            SET document_count = document_count + 1
            WHERE path = $1::ltree
            """,
            path,
        )

    async def recount_category(self, path: str) -> None:
        """Set document_count from current discovery_assets membership."""
        await self._pool.execute(
            """
            UPDATE category_taxonomy
            SET document_count = (
                SELECT COUNT(*)
                FROM discovery_assets
                WHERE category_path = $1::ltree
            )
            WHERE path = $1::ltree
            """,
            path,
        )

    async def search_by_path_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Search categories by ltree prefix match (ancestors or self)."""
        rows = await self._pool.fetch(
            """
            SELECT id, path::text AS path, name, description, embedding,
                   document_count, created_at
            FROM category_taxonomy
            WHERE path <@ $1::ltree
            ORDER BY path
            """,
            prefix,
        )
        return [dict(r) for r in rows]
