"""Tests for the FAISSIndex semantic vector index.

All tests run WITHOUT faiss-cpu installed by relying on the pure-Python
fallback path. This validates the core logic (search scoring, upsert staging,
rebuild, deletion, TTL expiry, metadata filtering) without requiring the
optional ML dependency.
"""

from __future__ import annotations

import asyncio
import math
import time
from fnmatch import fnmatch
from unittest.mock import AsyncMock, patch

import pytest

from interlock.cache.faiss_index import FAISSIndex, SemanticIndex, _dot, _normalize
from interlock.models import SemanticMatch

# ---------------------------------------------------------------------------
# Helpers - simple normalized test vectors
# ---------------------------------------------------------------------------


def _make_vector(dim: int, seed: float) -> list[float]:
    """Create a deterministic pseudo-random vector and normalize it."""
    raw = [math.sin(seed * (i + 1)) for i in range(dim)]
    return _normalize(raw)


DIM = 8  # Small dimension for fast tests


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.generations: dict[str, int] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str) -> bytes | None:
        if key in self.generations:
            return str(self.generations[key]).encode("ascii")
        return self.store.get(key)

    async def delete(self, *keys) -> int:
        deleted = 0
        for key in keys:
            key_str = key.decode() if isinstance(key, bytes) else str(key)
            if key_str in self.store:
                deleted += 1
                self.store.pop(key_str, None)
        return deleted

    async def scan(self, cursor=b"0", match: str | None = None, count: int = 200):
        keys = [key for key in self.store if match is None or fnmatch(key, match)]
        return b"0", keys

    async def incr(self, key: str) -> int:
        self.generations[key] = self.generations.get(key, 0) + 1
        return self.generations[key]

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_faiss_index_satisfies_protocol():
    """FAISSIndex should satisfy the SemanticIndex protocol."""
    assert isinstance(FAISSIndex(dimension=DIM), SemanticIndex)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_without_faiss():
    """When faiss is not available, should still initialize with fallback."""
    idx = FAISSIndex(dimension=DIM)
    with patch("interlock.cache.faiss_index._faiss", None):
        idx._use_faiss = False
        await idx.initialize()

    assert idx.available is True
    assert idx._index is None


@pytest.mark.asyncio
async def test_initialize_creates_available_index():
    """Default initialization (pure-Python fallback) sets available=True."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()
    assert idx.available is True


# ---------------------------------------------------------------------------
# Search when not available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_when_not_available():
    """search() returns empty list when not initialized."""
    idx = FAISSIndex(dimension=DIM)
    # Do NOT call initialize
    result = await idx.search([0.0] * DIM)
    assert result == []


@pytest.mark.asyncio
async def test_search_when_empty():
    """search() returns empty list when index has no entries."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()
    result = await idx.search([0.0] * DIM)
    assert result == []


# ---------------------------------------------------------------------------
# Upsert stages data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_stages_data():
    """upsert() should add to staged_upserts, not immediately to index."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    vec = _make_vector(DIM, 1.0)
    await idx.upsert("key1", vec, {"source": "test"})

    assert "key1" in idx._staged_upserts
    assert len(idx._keys) == 0  # Not yet in the live index


@pytest.mark.asyncio
async def test_upsert_with_ttl():
    """upsert() with ttl_seconds should compute an expiry timestamp."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    vec = _make_vector(DIM, 1.0)
    before = time.time()
    await idx.upsert("key1", vec, {}, ttl_seconds=60)

    _, _, expiry = idx._staged_upserts["key1"]
    assert expiry is not None
    assert expiry >= before + 59  # Allow 1s tolerance


# ---------------------------------------------------------------------------
# Rebuild applies staged upserts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_applies_staged_upserts():
    """After rebuild, staged entries should be in the live index."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    vec = _make_vector(DIM, 1.0)
    await idx.upsert("key1", vec, {"source": "test"})
    await idx.rebuild()

    assert "key1" in idx._keys
    assert len(idx._keys) == 1
    assert idx._metadata["key1"] == {"source": "test"}
    # Staging should be cleared
    assert len(idx._staged_upserts) == 0


@pytest.mark.asyncio
async def test_rebuild_clears_staging():
    """Rebuild should clear both staged upserts and deletes."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    await idx.upsert("a", _make_vector(DIM, 1.0), {})
    await idx.upsert("b", _make_vector(DIM, 2.0), {})
    await idx.rebuild()

    await idx.delete("a")
    assert "a" not in idx._staged_deletes

    await idx.rebuild()
    assert len(idx._staged_deletes) == 0
    assert len(idx._staged_upserts) == 0


# ---------------------------------------------------------------------------
# Search finds similar vectors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_finds_similar_vectors():
    """Search should return the most similar vector first."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    # Create three distinct vectors
    vec_a = _make_vector(DIM, 1.0)
    vec_b = _make_vector(DIM, 2.0)
    vec_c = _make_vector(DIM, 3.0)

    await idx.upsert("a", vec_a, {})
    await idx.upsert("b", vec_b, {})
    await idx.upsert("c", vec_c, {})
    await idx.rebuild()

    # Search for something very close to vec_a
    results = await idx.search(vec_a, top_k=3)

    assert len(results) == 3
    # First result should be key "a" (exact match, score ~1.0)
    assert results[0].key == "a"
    assert results[0].score > 0.99


@pytest.mark.asyncio
async def test_search_respects_top_k():
    """Search should return at most top_k results."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    for i in range(10):
        await idx.upsert(f"k{i}", _make_vector(DIM, float(i + 1)), {})
    await idx.rebuild()

    results = await idx.search(_make_vector(DIM, 1.0), top_k=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_search_scores_are_descending():
    """Results should be sorted by score descending."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    for i in range(5):
        await idx.upsert(f"k{i}", _make_vector(DIM, float(i + 1)), {})
    await idx.rebuild()

    results = await idx.search(_make_vector(DIM, 1.0), top_k=5)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Delete removes from index after rebuild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_after_rebuild():
    """Deleted keys should not appear in the index after rebuild."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    await idx.upsert("keep", _make_vector(DIM, 1.0), {})
    await idx.upsert("remove", _make_vector(DIM, 2.0), {})
    await idx.rebuild()

    assert await idx.count() == 2

    await idx.delete("remove")
    await idx.rebuild()

    assert await idx.count() == 1
    assert "remove" not in idx._keys
    assert "keep" in idx._keys


@pytest.mark.asyncio
async def test_delete_staged_upsert():
    """Deleting a key that was staged (not yet rebuilt) should remove it from staging."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    await idx.upsert("temp", _make_vector(DIM, 1.0), {})
    assert "temp" in idx._staged_upserts

    await idx.delete("temp")
    assert "temp" not in idx._staged_upserts
    assert "temp" not in idx._staged_deletes
    assert await idx.search(_make_vector(DIM, 1.0), top_k=5) == []


# ---------------------------------------------------------------------------
# Prune expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_expired_removes_entries():
    """prune_expired should remove entries whose TTL has passed."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    # Insert with already-expired TTL (ttl in the past)
    await idx.upsert("expired", _make_vector(DIM, 1.0), {}, ttl_seconds=1)
    await idx.upsert("valid", _make_vector(DIM, 2.0), {}, ttl_seconds=3600)
    await idx.rebuild()

    assert await idx.count() == 2

    # Manually set the expired entry's TTL to the past
    idx._ttls["expired"] = time.time() - 10

    removed = await idx.prune_expired()
    assert removed == 1
    assert await idx.count() == 1
    assert "expired" not in idx._keys
    assert "valid" in idx._keys


@pytest.mark.asyncio
async def test_prune_expired_returns_zero_when_nothing_expired():
    """prune_expired should return 0 when no entries are expired."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    await idx.upsert("valid", _make_vector(DIM, 1.0), {}, ttl_seconds=3600)
    await idx.rebuild()

    removed = await idx.prune_expired()
    assert removed == 0


@pytest.mark.asyncio
async def test_prune_not_available():
    """prune_expired returns 0 when index is not available."""
    idx = FAISSIndex(dimension=DIM)
    removed = await idx.prune_expired()
    assert removed == 0


# ---------------------------------------------------------------------------
# Count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_returns_correct_number():
    """count() should reflect the number of entries in the live index."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    assert await idx.count() == 0

    await idx.upsert("a", _make_vector(DIM, 1.0), {})
    await idx.upsert("b", _make_vector(DIM, 2.0), {})
    await idx.rebuild()

    assert await idx.count() == 2

    await idx.upsert("c", _make_vector(DIM, 3.0), {})
    await idx.rebuild()

    assert await idx.count() == 3


# ---------------------------------------------------------------------------
# Metadata filtering in search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_with_metadata_filter():
    """Search with filters should only return entries whose metadata matches."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    await idx.upsert("sql1", _make_vector(DIM, 1.0), {"source": "pg", "table": "users"})
    await idx.upsert("sql2", _make_vector(DIM, 1.1), {"source": "pg", "table": "orders"})
    await idx.upsert("http1", _make_vector(DIM, 1.2), {"source": "http", "table": "n/a"})
    await idx.rebuild()

    # Filter for source=pg
    results = await idx.search(_make_vector(DIM, 1.0), top_k=10, filters={"source": "pg"})
    assert len(results) == 2
    keys = {r.key for r in results}
    assert keys == {"sql1", "sql2"}


@pytest.mark.asyncio
async def test_search_filter_no_match():
    """Search with a filter that matches nothing should return empty list."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    await idx.upsert("a", _make_vector(DIM, 1.0), {"source": "pg"})
    await idx.rebuild()

    results = await idx.search(_make_vector(DIM, 1.0), top_k=10, filters={"source": "mysql"})
    assert results == []


@pytest.mark.asyncio
async def test_search_filter_multiple_conditions():
    """Filters with multiple keys should require ALL to match."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    await idx.upsert("a", _make_vector(DIM, 1.0), {"source": "pg", "table": "users"})
    await idx.upsert("b", _make_vector(DIM, 2.0), {"source": "pg", "table": "orders"})
    await idx.rebuild()

    results = await idx.search(
        _make_vector(DIM, 1.0),
        top_k=10,
        filters={"source": "pg", "table": "users"},
    )
    assert len(results) == 1
    assert results[0].key == "a"


# ---------------------------------------------------------------------------
# TTL expiry filtering in search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_skips_expired_entries():
    """Expired entries should not appear in search results even before prune."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    await idx.upsert("expired", _make_vector(DIM, 1.0), {}, ttl_seconds=1)
    await idx.upsert("valid", _make_vector(DIM, 1.1), {})
    await idx.rebuild()

    # Force the entry to be expired
    idx._ttls["expired"] = time.time() - 10

    results = await idx.search(_make_vector(DIM, 1.0), top_k=10)
    keys = {r.key for r in results}
    assert "expired" not in keys
    assert "valid" in keys


# ---------------------------------------------------------------------------
# Upsert overwrites existing key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_overwrites_on_rebuild():
    """Upserting an existing key should update its vector and metadata."""
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    vec1 = _make_vector(DIM, 1.0)
    await idx.upsert("key", vec1, {"version": 1})
    await idx.rebuild()

    assert idx._metadata["key"]["version"] == 1

    vec2 = _make_vector(DIM, 2.0)
    await idx.upsert("key", vec2, {"version": 2})
    await idx.rebuild()

    assert await idx.count() == 1
    assert idx._metadata["key"]["version"] == 2


# ---------------------------------------------------------------------------
# Redis persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_persists_to_redis():
    """upsert() should SET the entry in Redis when a client is available."""
    mock_redis = AsyncMock()
    idx = FAISSIndex(dimension=DIM, namespace="test", redis_client=mock_redis)
    await idx.initialize()

    vec = _make_vector(DIM, 1.0)
    await idx.upsert("rkey", vec, {"src": "pg"}, ttl_seconds=120)

    mock_redis.set.assert_called_once()
    call_args = mock_redis.set.call_args
    assert call_args[0][0] == "faiss:test:rkey"
    assert call_args[1]["ex"] == 120


@pytest.mark.asyncio
async def test_upsert_without_ttl_no_ex():
    """upsert() without TTL should SET without ex parameter."""
    mock_redis = AsyncMock()
    idx = FAISSIndex(dimension=DIM, namespace="test", redis_client=mock_redis)
    await idx.initialize()

    vec = _make_vector(DIM, 1.0)
    await idx.upsert("rkey", vec, {})

    call_args = mock_redis.set.call_args
    assert "ex" not in call_args[1]


@pytest.mark.asyncio
async def test_delete_removes_from_redis():
    """delete() should remove the key from Redis."""
    mock_redis = AsyncMock()
    idx = FAISSIndex(dimension=DIM, namespace="ns", redis_client=mock_redis)
    await idx.initialize()

    await idx.delete("gone")
    mock_redis.delete.assert_called_once_with("faiss:ns:gone")


# ---------------------------------------------------------------------------
# SemanticMatch model
# ---------------------------------------------------------------------------


def test_semantic_match_defaults():
    """SemanticMatch should have sensible defaults."""
    m = SemanticMatch(key="k", score=0.95)
    assert m.key == "k"
    assert m.score == 0.95
    assert m.metadata == {}


def test_semantic_match_with_metadata():
    """SemanticMatch should accept metadata."""
    m = SemanticMatch(key="k", score=0.8, metadata={"table": "users"})
    assert m.metadata["table"] == "users"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_normalize():
    """_normalize should produce a unit vector."""
    vec = [3.0, 4.0]
    normed = _normalize(vec)
    length = math.sqrt(sum(v * v for v in normed))
    assert abs(length - 1.0) < 1e-6


def test_normalize_zero_vector():
    """_normalize on a zero vector should return it unchanged."""
    vec = [0.0, 0.0, 0.0]
    normed = _normalize(vec)
    assert normed == vec


def test_dot_product():
    """_dot should compute the correct dot product."""
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert _dot(a, b) == 0.0

    c = [1.0, 2.0, 3.0]
    d = [4.0, 5.0, 6.0]
    assert _dot(c, d) == pytest.approx(32.0)


# ---------------------------------------------------------------------------
# Namespace isolation (no cross-talk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_separate_namespaces():
    """Two FAISSIndex instances with different namespaces are independent."""
    idx_cache = FAISSIndex(dimension=DIM, namespace="cache")
    idx_disc = FAISSIndex(dimension=DIM, namespace="discovery")
    await idx_cache.initialize()
    await idx_disc.initialize()

    await idx_cache.upsert("shared_key", _make_vector(DIM, 1.0), {"ns": "cache"})
    await idx_cache.rebuild()

    assert await idx_cache.count() == 1
    assert await idx_disc.count() == 0


# ---------------------------------------------------------------------------
# Generation snapshots and Redis generation reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_swaps_generation_snapshot_without_mixed_search_results():
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()

    vec_a = _make_vector(DIM, 1.0)
    vec_b = _make_vector(DIM, 2.0)
    await idx.upsert("old", vec_a, {"generation": "old"})
    await idx.rebuild()
    old_generation = idx.generation

    async def search_many() -> list[set[str]]:
        observed: list[set[str]] = []
        for _ in range(50):
            results = await idx.search(vec_a, top_k=10)
            observed.append({result.key for result in results})
        return observed

    search_task = asyncio.create_task(search_many())
    await idx.delete("old")
    await idx.upsert("new", vec_b, {"generation": "new"})
    await idx.rebuild()
    observed_sets = await search_task

    assert idx.generation > old_generation
    assert await idx.count() == 1
    assert idx._keys == ["new"]
    assert all(keys in ({"old"}, {"new"}) for keys in observed_sets if keys)


@pytest.mark.asyncio
async def test_reload_if_redis_generation_changed_replaces_snapshot():
    redis = FakeRedis()
    writer = FAISSIndex(dimension=DIM, namespace="cache", redis_client=redis)
    reader = FAISSIndex(dimension=DIM, namespace="cache", redis_client=redis)
    await writer.initialize()
    await reader.initialize()

    await writer.upsert("shared", _make_vector(DIM, 1.0), {"source_id": "src1"})
    await writer.rebuild()

    changed = await reader.reload_if_redis_generation_changed()
    assert changed is True
    assert reader.redis_generation == 1
    assert [match.key for match in await reader.search(_make_vector(DIM, 1.0), top_k=5)] == [
        "shared"
    ]

    await writer.delete("shared")
    changed = await reader.reload_if_redis_generation_changed()

    assert changed is True
    assert reader.redis_generation == 2
    assert await reader.search(_make_vector(DIM, 1.0), top_k=5) == []
    assert reader._keys == []
