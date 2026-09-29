"""Unit tests for L1 and L2 deterministic caches."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import msgpack
import pytest

from interlock.cache.l1 import L1Cache
from interlock.cache.l2 import L2Cache

# =========================================================================
# L1 Cache
# =========================================================================


class TestL1Cache:
    """Tests for the in-process L1 TTL cache."""

    @pytest.fixture()
    def cache(self) -> L1Cache:
        return L1Cache(max_size=100, ttl_seconds=60)

    @pytest.mark.asyncio
    async def test_get_miss(self, cache: L1Cache) -> None:
        result = await cache.get("nonexistent")
        assert result.hit is False
        assert result.data is None
        assert result.tier is None

    @pytest.mark.asyncio
    async def test_put_then_get_hit(self, cache: L1Cache) -> None:
        await cache.put("k1", b"hello")
        result = await cache.get("k1")
        assert result.hit is True
        assert result.data == b"hello"
        assert result.tier == "l1"

    @pytest.mark.asyncio
    async def test_ttl_expiry(self) -> None:
        """Verify entries expire after TTL by manipulating cachetools internals."""
        cache = L1Cache(max_size=100, ttl_seconds=1)
        await cache.put("k1", b"data")

        # Confirm it is there
        assert (await cache.get("k1")).hit is True

        # Wait for TTL to pass
        await asyncio.sleep(1.1)

        result = await cache.get("k1")
        assert result.hit is False

    @pytest.mark.asyncio
    async def test_invalidate(self, cache: L1Cache) -> None:
        await cache.put("k1", b"data")
        await cache.invalidate("k1")
        result = await cache.get("k1")
        assert result.hit is False

    @pytest.mark.asyncio
    async def test_invalidate_nonexistent(self, cache: L1Cache) -> None:
        # Should not raise
        await cache.invalidate("nope")

    @pytest.mark.asyncio
    async def test_clear(self, cache: L1Cache) -> None:
        await cache.put("a", b"1")
        await cache.put("b", b"2")
        await cache.clear()
        assert (await cache.get("a")).hit is False
        assert (await cache.get("b")).hit is False

    @pytest.mark.asyncio
    async def test_stats_tracking(self, cache: L1Cache) -> None:
        # Initial state
        stats = cache.stats
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["size"] == 0
        assert stats["max_size"] == 100

        # Miss
        await cache.get("x")
        assert cache.stats["misses"] == 1

        # Put + hit
        await cache.put("x", b"val")
        await cache.get("x")
        assert cache.stats["hits"] == 1
        assert cache.stats["size"] == 1

    @pytest.mark.asyncio
    async def test_eviction_at_max_size(self) -> None:
        cache = L1Cache(max_size=2, ttl_seconds=60)
        await cache.put("a", b"1")
        await cache.put("b", b"2")
        await cache.put("c", b"3")  # should evict oldest
        assert cache.stats["size"] == 2


# =========================================================================
# L2 Cache
# =========================================================================


class TestL2Cache:
    """Tests for the Redis-backed L2 cache."""

    @pytest.fixture()
    def mock_redis(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture()
    def cache(self, mock_redis: AsyncMock) -> L2Cache:
        return L2Cache(redis_client=mock_redis, ttl_seconds=300, key_prefix="test:")

    @pytest.mark.asyncio
    async def test_get_miss(self, cache: L2Cache, mock_redis: AsyncMock) -> None:
        mock_redis.get.return_value = None
        result = await cache.get("k1")
        assert result.hit is False
        assert result.data is None
        mock_redis.get.assert_awaited_once_with("test:k1")

    @pytest.mark.asyncio
    async def test_put_then_get_hit(self, cache: L2Cache, mock_redis: AsyncMock) -> None:
        # Put
        await cache.put("k1", b"hello", metadata={"source": "test"})
        mock_redis.set.assert_awaited_once()
        call_args = mock_redis.set.call_args
        stored_key = call_args[0][0]
        stored_payload = call_args[0][1]
        assert stored_key == "test:k1"

        # Simulate Redis returning what was stored
        mock_redis.get.return_value = stored_payload
        result = await cache.get("k1")
        assert result.hit is True
        assert result.data == b"hello"
        assert result.tier == "l2"
        assert result.metadata["source"] == "test"

    @pytest.mark.asyncio
    async def test_put_stores_msgpack_with_metadata(
        self, cache: L2Cache, mock_redis: AsyncMock
    ) -> None:
        await cache.put("k1", b"data", metadata={"x": 1})
        raw = mock_redis.set.call_args[0][1]
        payload = msgpack.unpackb(raw, raw=False)
        assert payload["data"] == b"data"
        assert payload["metadata"] == {"x": 1}
        assert "stored_at" in payload

    @pytest.mark.asyncio
    async def test_put_default_metadata_is_empty(
        self, cache: L2Cache, mock_redis: AsyncMock
    ) -> None:
        await cache.put("k1", b"data")
        raw = mock_redis.set.call_args[0][1]
        payload = msgpack.unpackb(raw, raw=False)
        assert payload["metadata"] == {}

    @pytest.mark.asyncio
    async def test_put_uses_ttl(self, cache: L2Cache, mock_redis: AsyncMock) -> None:
        await cache.put("k1", b"data")
        call_kwargs = mock_redis.set.call_args[1]
        assert call_kwargs["ex"] == 300

    @pytest.mark.asyncio
    async def test_invalidate(self, cache: L2Cache, mock_redis: AsyncMock) -> None:
        await cache.invalidate("k1")
        mock_redis.delete.assert_awaited_once_with("test:k1")

    @pytest.mark.asyncio
    async def test_redis_get_error_returns_miss(
        self, cache: L2Cache, mock_redis: AsyncMock
    ) -> None:
        mock_redis.get.side_effect = ConnectionError("Redis down")
        result = await cache.get("k1")
        assert result.hit is False

    @pytest.mark.asyncio
    async def test_redis_set_error_does_not_raise(
        self, cache: L2Cache, mock_redis: AsyncMock
    ) -> None:
        mock_redis.set.side_effect = ConnectionError("Redis down")
        # Should not raise
        await cache.put("k1", b"data")

    @pytest.mark.asyncio
    async def test_redis_delete_error_does_not_raise(
        self, cache: L2Cache, mock_redis: AsyncMock
    ) -> None:
        mock_redis.delete.side_effect = ConnectionError("Redis down")
        # Should not raise
        await cache.invalidate("k1")

    @pytest.mark.asyncio
    async def test_stats_tracking(self, cache: L2Cache, mock_redis: AsyncMock) -> None:
        # Miss
        mock_redis.get.return_value = None
        await cache.get("k1")
        assert cache.stats["misses"] == 1
        assert cache.stats["hits"] == 0

        # Hit
        payload = msgpack.packb({"data": b"x", "metadata": {}, "stored_at": 0.0}, use_bin_type=True)
        mock_redis.get.return_value = payload
        await cache.get("k1")
        assert cache.stats["hits"] == 1

    @pytest.mark.asyncio
    async def test_stats_increments_miss_on_redis_error(
        self, cache: L2Cache, mock_redis: AsyncMock
    ) -> None:
        mock_redis.get.side_effect = ConnectionError("down")
        await cache.get("k1")
        assert cache.stats["misses"] == 1
