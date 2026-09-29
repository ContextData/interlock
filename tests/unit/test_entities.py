"""Unit tests for EntityManager."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.discovery.entities import EntityManager


def _mock_pool_with_conn(conn: AsyncMock) -> AsyncMock:
    """Create a mock pool whose acquire() yields conn as an async context manager."""
    pool = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


# ---------------------------------------------------------------------------
# normalize_entity
# ---------------------------------------------------------------------------


class TestNormalizeEntity:
    def test_lowercase(self):
        assert EntityManager.normalize_entity("Hello World") == "hello world"

    def test_strip_whitespace(self):
        assert EntityManager.normalize_entity("  foo  ") == "foo"

    def test_collapse_spaces(self):
        assert EntityManager.normalize_entity("a   b   c") == "a b c"

    def test_combined(self):
        assert EntityManager.normalize_entity("  Hello   World  ") == "hello world"

    def test_empty_string(self):
        assert EntityManager.normalize_entity("") == ""

    def test_single_word(self):
        assert EntityManager.normalize_entity("Python") == "python"


# ---------------------------------------------------------------------------
# store_entities
# ---------------------------------------------------------------------------


class TestStoreEntities:
    @pytest.mark.asyncio
    async def test_store_entities_basic(self):
        conn = AsyncMock()
        conn.execute = AsyncMock()
        pool = _mock_pool_with_conn(conn)
        manager = EntityManager(pool)

        entities = [
            {"text": "Python", "type": "language", "prominence": 0.9},
            {"text": "FastAPI", "type": "framework", "prominence": 0.7},
        ]

        count = await manager.store_entities(document_id=1, entities=entities)

        assert count == 2
        assert conn.execute.call_count == 2
        # Check the first call used normalized text
        first_call_args = conn.execute.call_args_list[0]
        assert first_call_args[0][1] == "python"  # normalized
        assert first_call_args[0][2] == "language"
        assert first_call_args[0][3] == 1  # document_id
        assert first_call_args[0][4] == 0.9  # prominence

    @pytest.mark.asyncio
    async def test_store_entities_empty_list(self):
        pool = AsyncMock()
        manager = EntityManager(pool)
        count = await manager.store_entities(document_id=1, entities=[])
        assert count == 0

    @pytest.mark.asyncio
    async def test_store_entities_skips_empty_text(self):
        conn = AsyncMock()
        conn.execute = AsyncMock()
        pool = _mock_pool_with_conn(conn)
        manager = EntityManager(pool)

        entities = [
            {"text": "   ", "type": "unknown", "prominence": 0.5},
        ]

        count = await manager.store_entities(document_id=1, entities=entities)
        assert count == 0
        conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# get_entities_for_document
# ---------------------------------------------------------------------------


class TestGetEntitiesForDocument:
    @pytest.mark.asyncio
    async def test_returns_dicts(self):
        pool = AsyncMock()
        row1 = MagicMock()
        row1.__iter__ = MagicMock(return_value=iter([]))
        row1.items = MagicMock(
            return_value=[
                ("entity_text", "python"),
                ("entity_type", "language"),
                ("prominence", 0.9),
                ("metadata", {}),
            ]
        )
        row1.keys = MagicMock(return_value=["entity_text", "entity_type", "prominence", "metadata"])
        row1.__getitem__ = lambda self, k: dict(self.items())[k]

        pool.fetch = AsyncMock(
            return_value=[
                {
                    "entity_text": "python",
                    "entity_type": "language",
                    "prominence": 0.9,
                    "metadata": {},
                }
            ]
        )

        manager = EntityManager(pool)
        result = await manager.get_entities_for_document(1)
        assert len(result) == 1
        pool.fetch.assert_called_once()


# ---------------------------------------------------------------------------
# get_related_documents
# ---------------------------------------------------------------------------


class TestGetRelatedDocuments:
    @pytest.mark.asyncio
    async def test_calls_with_correct_params(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])

        manager = EntityManager(pool)
        result = await manager.get_related_documents(document_id=42, limit=5)

        assert result == []
        pool.fetch.assert_called_once()
        call_args = pool.fetch.call_args
        assert call_args[0][1] == 42  # document_id
        assert call_args[0][2] == 5  # limit


# ---------------------------------------------------------------------------
# get_co_occurring_entities
# ---------------------------------------------------------------------------


class TestGetCoOccurringEntities:
    @pytest.mark.asyncio
    async def test_normalizes_input(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])

        manager = EntityManager(pool)
        await manager.get_co_occurring_entities("  Python  ", limit=10)

        call_args = pool.fetch.call_args
        assert call_args[0][1] == "python"  # normalized
        assert call_args[0][2] == 10  # limit

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])

        manager = EntityManager(pool)
        result = await manager.get_co_occurring_entities("nonexistent")
        assert result == []
