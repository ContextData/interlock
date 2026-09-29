"""Tests for CategoryManager - ltree-based hierarchical category taxonomy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.discovery.category import CategoryManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool() -> AsyncMock:
    """Create a mock asyncpg.Pool with async methods."""
    pool = AsyncMock()
    return pool


def _make_row(data: dict) -> MagicMock:
    """Create a mock asyncpg.Record that supports both dict() and key access."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.keys = lambda: data.keys()
    row.values = lambda: data.values()
    row.items = lambda: data.items()
    # Make dict(row) work
    row.__iter__ = lambda self: iter(data)
    row.__len__ = lambda self: len(data)
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateCategory:
    @pytest.mark.asyncio
    async def test_create_new_category(self) -> None:
        pool = _make_pool()
        pool.fetchrow.return_value = _make_row({"id": 42})

        mgr = CategoryManager(pool)
        result = await mgr.create_category("engineering.backend", "Backend")

        assert result == 42
        pool.fetchrow.assert_called_once()
        call_args = pool.fetchrow.call_args
        assert "INSERT INTO category_taxonomy" in call_args[0][0]
        assert call_args[0][1] == "engineering.backend"
        assert call_args[0][2] == "Backend"

    @pytest.mark.asyncio
    async def test_create_existing_category_returns_existing_id(self) -> None:
        pool = _make_pool()
        # First fetchrow (INSERT) returns None (conflict, DO NOTHING)
        # Second fetchrow (SELECT) returns the existing row
        pool.fetchrow.side_effect = [None, _make_row({"id": 7})]

        mgr = CategoryManager(pool)
        result = await mgr.create_category("data.ml", "ML")

        assert result == 7
        assert pool.fetchrow.call_count == 2

    @pytest.mark.asyncio
    async def test_create_with_optional_fields(self) -> None:
        pool = _make_pool()
        pool.fetchrow.return_value = _make_row({"id": 1})

        mgr = CategoryManager(pool)
        embedding = [0.1, 0.2, 0.3]
        result = await mgr.create_category(
            "docs.api", "API Docs", description="API documentation", embedding=embedding
        )

        assert result == 1
        call_args = pool.fetchrow.call_args
        assert call_args[0][3] == "API documentation"
        assert call_args[0][4] == embedding


class TestGetChildren:
    @pytest.mark.asyncio
    async def test_get_children(self) -> None:
        pool = _make_pool()
        child1 = _make_row(
            {
                "id": 2,
                "path": "eng.backend",
                "name": "Backend",
                "description": None,
                "embedding": None,
                "document_count": 5,
                "created_at": "2026-01-01",
            }
        )
        child2 = _make_row(
            {
                "id": 3,
                "path": "eng.frontend",
                "name": "Frontend",
                "description": None,
                "embedding": None,
                "document_count": 3,
                "created_at": "2026-01-01",
            }
        )
        pool.fetch.return_value = [child1, child2]

        mgr = CategoryManager(pool)
        children = await mgr.get_children("eng")

        assert len(children) == 2
        assert children[0]["path"] == "eng.backend"
        assert children[1]["name"] == "Frontend"
        # Verify lquery pattern is used
        call_args = pool.fetch.call_args
        assert "lquery" in call_args[0][0]
        assert call_args[0][1] == "eng"


class TestGetSubtree:
    @pytest.mark.asyncio
    async def test_get_subtree(self) -> None:
        pool = _make_pool()
        rows = [
            _make_row(
                {
                    "id": i,
                    "path": p,
                    "name": p.split(".")[-1],
                    "description": None,
                    "embedding": None,
                    "document_count": 0,
                    "created_at": "2026-01-01",
                }
            )
            for i, p in enumerate(["eng", "eng.backend", "eng.backend.api"], start=1)
        ]
        pool.fetch.return_value = rows

        mgr = CategoryManager(pool)
        subtree = await mgr.get_subtree("eng")

        assert len(subtree) == 3
        assert subtree[0]["path"] == "eng"
        assert subtree[2]["path"] == "eng.backend.api"
        # Verify <@ operator is used
        call_args = pool.fetch.call_args
        assert "<@" in call_args[0][0]


class TestGetRoots:
    @pytest.mark.asyncio
    async def test_get_roots(self) -> None:
        pool = _make_pool()
        rows = [
            _make_row(
                {
                    "id": 1,
                    "path": "engineering",
                    "name": "Engineering",
                    "description": None,
                    "embedding": None,
                    "document_count": 10,
                    "created_at": "2026-01-01",
                }
            ),
            _make_row(
                {
                    "id": 2,
                    "path": "marketing",
                    "name": "Marketing",
                    "description": None,
                    "embedding": None,
                    "document_count": 5,
                    "created_at": "2026-01-01",
                }
            ),
        ]
        pool.fetch.return_value = rows

        mgr = CategoryManager(pool)
        roots = await mgr.get_roots()

        assert len(roots) == 2
        assert roots[0]["name"] == "Engineering"
        # Verify nlevel(path) = 1 query
        call_args = pool.fetch.call_args
        assert "nlevel" in call_args[0][0]


class TestAssignDocument:
    @pytest.mark.asyncio
    async def test_assign_document(self) -> None:
        pool = _make_pool()
        pool.execute.return_value = None

        mgr = CategoryManager(pool)
        await mgr.assign_document(document_id=99, category_path="eng.backend")

        pool.execute.assert_called_once()
        call_args = pool.execute.call_args
        assert "UPDATE discovery_assets" in call_args[0][0]
        assert call_args[0][1] == "eng.backend"
        assert call_args[0][2] == 99


class TestIncrementCount:
    @pytest.mark.asyncio
    async def test_increment_count(self) -> None:
        pool = _make_pool()
        pool.execute.return_value = None

        mgr = CategoryManager(pool)
        await mgr.increment_count("eng.backend")

        pool.execute.assert_called_once()
        call_args = pool.execute.call_args
        assert "document_count = document_count + 1" in call_args[0][0]
        assert call_args[0][1] == "eng.backend"
