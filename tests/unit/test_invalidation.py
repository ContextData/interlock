"""Unit tests for cache invalidation on writes (P3-T06)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from interlock.cache.invalidation import INVALIDATION_CHANNEL, CacheInvalidator
from interlock.models import SemanticMatch

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_l1() -> AsyncMock:
    l1 = AsyncMock()
    l1.clear = AsyncMock()
    # stats returns a dict with "size"
    l1.stats = {"size": 5, "hits": 0, "misses": 0, "max_size": 100}
    return l1


@pytest.fixture()
def mock_l2() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_semantic() -> AsyncMock:
    si = AsyncMock()
    si._dimension = 384
    si.search = AsyncMock(return_value=[])
    si.delete = AsyncMock()
    return si


@pytest.fixture()
def mock_redis() -> AsyncMock:
    return AsyncMock()


class FakePubSub:
    def __init__(self, messages: list[dict] | None = None, *, fail_once: bool = False) -> None:
        self.messages = list(messages or [])
        self.fail_once = fail_once
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()
        self.aclose = AsyncMock()

    async def get_message(self, *args, **kwargs):
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("pubsub dropped")
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(0.01)
        return None


class FakeRedis:
    def __init__(self, pubsubs: list[FakePubSub]) -> None:
        self.pubsubs = pubsubs
        self.created = 0

    def pubsub(self):
        pubsub = self.pubsubs[self.created]
        self.created += 1
        return pubsub


# =========================================================================
# invalidate_for_tables
# =========================================================================


class TestInvalidateForTables:
    """Test table-level cache invalidation."""

    @pytest.mark.asyncio
    async def test_clears_l1(self, mock_l1: AsyncMock, mock_redis: AsyncMock) -> None:
        invalidator = CacheInvalidator(l1=mock_l1, redis_client=mock_redis)
        count = await invalidator.invalidate_for_tables("src1", ["users", "orders"])

        mock_l1.clear.assert_awaited_once()
        # Count should include the 5 entries from L1 stats
        assert count >= 5

    @pytest.mark.asyncio
    async def test_publishes_to_redis(self, mock_l1: AsyncMock, mock_redis: AsyncMock) -> None:
        invalidator = CacheInvalidator(l1=mock_l1, redis_client=mock_redis)
        await invalidator.invalidate_for_tables("src1", ["users"])

        mock_redis.publish.assert_awaited_once()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == INVALIDATION_CHANNEL
        payload = json.loads(call_args[0][1])
        assert payload["source_id"] == "src1"
        assert payload["tables"] == ["users"]
        assert payload["version"] == 2
        assert payload["cache_keys"] == []

    @pytest.mark.asyncio
    async def test_records_cache_dependencies_in_database(self) -> None:
        pool = AsyncMock()
        invalidator = CacheInvalidator(pg_pool=pool)

        await invalidator.record_dependency(
            "cache-key-1",
            source_id="src1",
            tables=["users", "orders", "users"],
            protocol="pg",
            metadata={"query": "safe"},
            ttl_seconds=60,
        )

        # One delete+insert pair per unique table dimension.
        assert pool.execute.await_count == 4
        insert_calls = [
            call
            for call in pool.execute.await_args_list
            if "INSERT INTO cache_dependencies" in call.args[0]
        ]
        assert len(insert_calls) == 2
        assert {call.args[3] for call in insert_calls} == {"orders", "users"}

    @pytest.mark.asyncio
    async def test_dependency_keys_invalidate_exact_l1_l2_entries(
        self,
        mock_l1: AsyncMock,
        mock_l2: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        mock_l2.invalidate = AsyncMock()
        invalidator = CacheInvalidator(
            l1=mock_l1,
            l2=mock_l2,
            redis_client=mock_redis,
            instance_id="gw-a",
        )
        await invalidator.record_dependency(
            "cache-key-1",
            source_id="src1",
            tables=["users"],
            protocol="pg",
        )

        count = await invalidator.invalidate_for_tables("src1", ["users"])

        mock_l1.invalidate.assert_awaited_once_with("cache-key-1")
        mock_l1.clear.assert_not_awaited()
        mock_l2.invalidate.assert_awaited_once_with("cache-key-1")
        assert count == 2
        payload = json.loads(mock_redis.publish.await_args.args[1])
        assert payload["cache_keys"] == ["cache-key-1"]
        assert payload["source_instance"] == "gw-a"

    @pytest.mark.asyncio
    async def test_deletes_semantic_entries(
        self,
        mock_l1: AsyncMock,
        mock_semantic: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        mock_semantic.search.return_value = [
            SemanticMatch(key="entry1", score=0.5, metadata={"source_id": "src1"}),
            SemanticMatch(key="entry2", score=0.3, metadata={"source_id": "src1"}),
        ]
        invalidator = CacheInvalidator(
            l1=mock_l1,
            semantic_index=mock_semantic,
            redis_client=mock_redis,
        )

        count = await invalidator.invalidate_for_tables("src1", ["users"])

        # 5 from L1 + 2 from semantic
        assert count == 7
        assert mock_semantic.delete.await_count == 2
        mock_semantic.delete.assert_any_await("entry1")
        mock_semantic.delete.assert_any_await("entry2")


# =========================================================================
# invalidate_for_source
# =========================================================================


class TestInvalidateForSource:
    """Test source-level cache invalidation."""

    @pytest.mark.asyncio
    async def test_clears_l1(self, mock_l1: AsyncMock, mock_redis: AsyncMock) -> None:
        invalidator = CacheInvalidator(l1=mock_l1, redis_client=mock_redis)
        count = await invalidator.invalidate_for_source("src1")

        mock_l1.clear.assert_awaited_once()
        assert count >= 5

    @pytest.mark.asyncio
    async def test_publishes_with_empty_tables(
        self, mock_l1: AsyncMock, mock_redis: AsyncMock
    ) -> None:
        invalidator = CacheInvalidator(l1=mock_l1, redis_client=mock_redis)
        await invalidator.invalidate_for_source("src1")

        mock_redis.publish.assert_awaited_once()
        payload = json.loads(mock_redis.publish.call_args[0][1])
        assert payload["tables"] == []


# =========================================================================
# publish_invalidation
# =========================================================================


class TestPublishInvalidation:
    """Test Redis pub/sub publishing."""

    @pytest.mark.asyncio
    async def test_publishes_json_to_channel(self, mock_redis: AsyncMock) -> None:
        invalidator = CacheInvalidator(redis_client=mock_redis)

        await invalidator.publish_invalidation("src1", ["users", "orders"])

        mock_redis.publish.assert_awaited_once()
        channel, message = mock_redis.publish.call_args[0]
        assert channel == "onyx:cache_invalidate"
        parsed = json.loads(message)
        assert parsed["source_id"] == "src1"
        assert parsed["tables"] == ["users", "orders"]
        assert parsed["generation"] == 1

    @pytest.mark.asyncio
    async def test_remote_invalidation_uses_published_keys_without_full_clear(
        self,
        mock_l1: AsyncMock,
        mock_l2: AsyncMock,
    ) -> None:
        mock_l2.invalidate = AsyncMock()
        invalidator = CacheInvalidator(l1=mock_l1, l2=mock_l2, instance_id="gw-b")

        count = await invalidator.apply_remote_invalidation(
            {
                "source_id": "src1",
                "tables": ["users"],
                "cache_keys": ["cache-key-1"],
                "generation": 1,
                "source_instance": "gw-a",
            }
        )

        mock_l1.invalidate.assert_awaited_once_with("cache-key-1")
        mock_l1.clear.assert_not_awaited()
        mock_l2.invalidate.assert_awaited_once_with("cache-key-1")
        assert count == 2
        assert await invalidator.source_generation("src1") == 1

    @pytest.mark.asyncio
    async def test_remote_invalidation_skips_own_instance(self, mock_l1: AsyncMock) -> None:
        invalidator = CacheInvalidator(l1=mock_l1, instance_id="gw-a")

        count = await invalidator.apply_remote_invalidation(
            {
                "source_id": "src1",
                "cache_keys": ["cache-key-1"],
                "generation": 1,
                "source_instance": "gw-a",
            }
        )

        assert count == 0
        mock_l1.invalidate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prepare_source_write_returns_waitable_generation_token(self) -> None:
        invalidator = CacheInvalidator()

        token = await invalidator.prepare_source_write("src1")

        assert token.source_id == "src1"
        assert token.generation == 1
        assert await invalidator.source_generation("src1") == 1
        assert await invalidator.wait_for_source_generation(token, timeout_seconds=0.01) is True

    @pytest.mark.asyncio
    async def test_publish_can_reuse_pre_write_generation(
        self,
        mock_redis: AsyncMock,
    ) -> None:
        invalidator = CacheInvalidator(redis_client=mock_redis)
        token = await invalidator.prepare_source_write("src1")

        await invalidator.publish_invalidation(
            "src1",
            ["users"],
            source_generation=token.generation,
        )

        payload = json.loads(mock_redis.publish.await_args.args[1])
        assert payload["generation"] == token.generation
        assert await invalidator.source_generation("src1") == token.generation

    @pytest.mark.asyncio
    async def test_generation_gap_forces_conservative_full_resync(
        self,
        mock_l1: AsyncMock,
        mock_l2: AsyncMock,
    ) -> None:
        mock_l2.clear = AsyncMock(return_value=3)
        invalidator = CacheInvalidator(l1=mock_l1, l2=mock_l2)
        invalidator._last_seen_source_generation["src1"] = 1
        await invalidator.record_dependency("cache-key-1", source_id="src1", tables=["users"])

        count = await invalidator.apply_remote_invalidation(
            {
                "source_id": "src1",
                "tables": ["users"],
                "cache_keys": ["cache-key-1"],
                "generation": 3,
                "source_instance": "gw-a",
            }
        )

        mock_l1.clear.assert_awaited_once()
        mock_l1.invalidate.assert_not_awaited()
        mock_l2.clear.assert_awaited_once()
        mock_l2.invalidate.assert_not_awaited()
        assert count == 8
        assert await invalidator.source_generation("src1") == 3

    @pytest.mark.asyncio
    async def test_listener_reconnects_after_pubsub_error(self, mock_l1: AsyncMock) -> None:
        first = FakePubSub(fail_once=True)
        second = FakePubSub(
            [
                {
                    "data": json.dumps(
                        {
                            "source_id": "src1",
                            "cache_keys": ["cache-key-1"],
                            "generation": 1,
                            "source_instance": "gw-a",
                        }
                    )
                }
            ]
        )
        redis = FakeRedis([first, second])
        invalidator = CacheInvalidator(
            l1=mock_l1,
            redis_client=redis,
            instance_id="gw-b",
            reconnect_backoff_seconds=0,
        )

        await invalidator.start_listener()
        try:
            for _ in range(50):
                if redis.created >= 2 and mock_l1.invalidate.await_count:
                    break
                await asyncio.sleep(0.01)
        finally:
            await invalidator.stop_listener()

        assert redis.created >= 2
        # The dropped connection is closed, not sent UNSUBSCRIBE: that command
        # fails while Redis is down and used to end the listener for good.
        first.unsubscribe.assert_not_awaited()
        first.aclose.assert_awaited_once()
        mock_l1.invalidate.assert_awaited_once_with("cache-key-1")

    @pytest.mark.asyncio
    async def test_no_redis_is_noop(self) -> None:
        invalidator = CacheInvalidator()
        # Should not raise
        await invalidator.publish_invalidation("src1", ["users"])

    @pytest.mark.asyncio
    async def test_redis_error_does_not_raise(self, mock_redis: AsyncMock) -> None:
        mock_redis.publish.side_effect = ConnectionError("Redis down")
        invalidator = CacheInvalidator(redis_client=mock_redis)
        # Should not raise
        await invalidator.publish_invalidation("src1", ["users"])


# =========================================================================
# Edge cases
# =========================================================================


class TestEdgeCases:
    """Edge case handling."""

    @pytest.mark.asyncio
    async def test_no_caches_configured(self) -> None:
        """Invalidator with no caches should return 0."""
        invalidator = CacheInvalidator()
        count = await invalidator.invalidate_for_tables("src1", ["users"])
        assert count == 0

    @pytest.mark.asyncio
    async def test_semantic_search_error_returns_zero(
        self, mock_l1: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        mock_semantic.search.side_effect = RuntimeError("index corrupt")
        invalidator = CacheInvalidator(l1=mock_l1, semantic_index=mock_semantic)
        # Should not raise; semantic count is 0
        count = await invalidator.invalidate_for_source("src1")
        assert count == 5  # only L1 entries
