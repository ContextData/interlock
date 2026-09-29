"""Tests for discovery vector generation publish/reload hooks."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from interlock.discovery.vector_sync import (
    FAISS_REBUILD_CHANNEL,
    publish_vector_generation,
    reload_index_if_generation_changed,
)
from interlock.worker.indexer import DocumentIndexer
from interlock.worker.summarizer import DocumentSummary


@pytest.mark.asyncio
async def test_publish_vector_generation_increments_and_broadcasts() -> None:
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=7)
    redis.publish = AsyncMock()

    generation = await publish_vector_generation(
        redis,
        namespace="discovery",
        source_id="src1",
        asset_path="s3://bucket/doc.md",
        asset_id=42,
    )

    assert generation == 7
    redis.incr.assert_awaited_once_with("onyx:faiss:generation:discovery")
    redis.publish.assert_awaited_once()
    channel, payload = redis.publish.await_args.args
    assert channel == FAISS_REBUILD_CHANNEL
    parsed = json.loads(payload)
    assert parsed["event"] == "index_updated"
    assert parsed["namespace"] == "discovery"
    assert parsed["generation"] == 7
    assert parsed["asset_id"] == 42


@pytest.mark.asyncio
async def test_reload_index_if_generation_changed_loads_once() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"2")

    class FakeIndex:
        def __init__(self) -> None:
            self._redis = redis
            self._load_from_redis = AsyncMock()

    index = FakeIndex()

    changed = await reload_index_if_generation_changed(index, namespace="discovery")
    unchanged = await reload_index_if_generation_changed(index, namespace="discovery")

    assert changed is True
    assert unchanged is False
    index._load_from_redis.assert_awaited_once()


@pytest.mark.asyncio
async def test_document_indexer_publishes_after_semantic_upsert() -> None:
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={"id": 42})
    pool.execute = AsyncMock()

    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=3)
    redis.publish = AsyncMock()

    semantic_index = AsyncMock()
    semantic_index.upsert = AsyncMock()
    semantic_index._redis = redis
    semantic_index._namespace = "discovery"

    indexer = DocumentIndexer(pg_pool=pool, semantic_index=semantic_index)
    await indexer.index_document(
        source_id="src1",
        asset_path="s3://bucket/doc.md",
        asset_type="file",
        content="hello",
        summary=DocumentSummary(title="Doc", summary="hello"),
        embedding=[0.1, 0.2, 0.3],
    )

    semantic_index.upsert.assert_awaited_once()
    redis.incr.assert_awaited_once_with("onyx:faiss:generation:discovery")
    redis.publish.assert_awaited_once()
