"""Unit tests for multi-instance gateway coordination."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.cache.invalidation import CacheInvalidator
from interlock.gateway.multi_instance import (
    INSTANCE_KEY_PREFIX,
    GatewayInstanceManager,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_redis() -> AsyncMock:
    """Return an AsyncMock that behaves like redis.asyncio.Redis."""
    r = AsyncMock()
    r.set = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.delete = AsyncMock()
    r.publish = AsyncMock()
    r.scan_iter = MagicMock()  # async generator is set per-test
    return r


@pytest.fixture()
def manager(mock_redis: AsyncMock) -> GatewayInstanceManager:
    return GatewayInstanceManager(
        instance_id="gw-test01",
        redis_client=mock_redis,
        heartbeat_interval=1.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInstanceIdGeneration:
    def test_explicit_id(self, mock_redis: AsyncMock) -> None:
        mgr = GatewayInstanceManager(instance_id="gw-custom", redis_client=mock_redis)
        assert mgr.instance_id == "gw-custom"

    def test_auto_generated_id(self, mock_redis: AsyncMock) -> None:
        mgr = GatewayInstanceManager(redis_client=mock_redis)
        assert mgr.instance_id.startswith("gw-")
        assert len(mgr.instance_id) == 11  # "gw-" + 8 hex chars

    def test_no_redis_still_has_id(self) -> None:
        mgr = GatewayInstanceManager()
        assert mgr.instance_id.startswith("gw-")


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_registers_in_redis(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        await manager.start()
        try:
            # Verify registration was called
            mock_redis.set.assert_called_once()
            call_args = mock_redis.set.call_args
            key = call_args[0][0]
            data = json.loads(call_args[0][1])

            assert key == f"{INSTANCE_KEY_PREFIX}gw-test01"
            assert data["instance_id"] == "gw-test01"
            assert "started_at" in data
            assert "last_heartbeat" in data
            assert manager.started is True
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_stop_deregisters(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        await manager.start()
        mock_redis.set.reset_mock()

        await manager.stop()
        mock_redis.delete.assert_called_once_with(f"{INSTANCE_KEY_PREFIX}gw-test01")
        assert manager.started is False

    @pytest.mark.asyncio
    async def test_start_without_redis_is_noop(self) -> None:
        mgr = GatewayInstanceManager(instance_id="gw-nope")
        await mgr.start()
        assert mgr.started is False
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        await manager.start()
        try:
            call_count = mock_redis.set.call_count
            await manager.start()  # second call should be no-op
            assert mock_redis.set.call_count == call_count
        finally:
            await manager.stop()


class TestGetActiveInstances:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_instances(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        async def _empty_scan(*a, **kw):
            return
            yield  # make it an async generator  # noqa: E501

        mock_redis.scan_iter = _empty_scan
        result = await manager.get_active_instances()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_registered_instances(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        instance_data = json.dumps(
            {
                "instance_id": "gw-abc123",
                "started_at": 1000.0,
                "last_heartbeat": 1010.0,
            }
        )

        async def _scan_iter(*a, **kw):
            yield f"{INSTANCE_KEY_PREFIX}gw-abc123"

        mock_redis.scan_iter = _scan_iter
        mock_redis.get = AsyncMock(return_value=instance_data)

        result = await manager.get_active_instances()
        assert len(result) == 1
        assert result[0]["instance_id"] == "gw-abc123"

    @pytest.mark.asyncio
    async def test_skips_invalid_json(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        async def _scan_iter(*a, **kw):
            yield f"{INSTANCE_KEY_PREFIX}gw-bad"

        mock_redis.scan_iter = _scan_iter
        mock_redis.get = AsyncMock(return_value="not-json{{")

        result = await manager.get_active_instances()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_without_redis(self) -> None:
        mgr = GatewayInstanceManager()
        result = await mgr.get_active_instances()
        assert result == []


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_updates_timestamp(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        await manager.start()
        try:
            # Initial registration call
            initial_count = mock_redis.set.call_count
            assert initial_count == 1

            # Wait for at least one heartbeat cycle (interval is 1s)
            await asyncio.sleep(1.5)

            # Should have at least one more set call from heartbeat
            assert mock_redis.set.call_count > initial_count

            # Verify the heartbeat wrote the correct key
            last_call = mock_redis.set.call_args
            key = last_call[0][0]
            assert key == f"{INSTANCE_KEY_PREFIX}gw-test01"
        finally:
            await manager.stop()


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_cache_invalidate(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        await manager.broadcast_cache_invalidate(["key1", "key2"])
        mock_redis.publish.assert_called_once()
        channel = mock_redis.publish.call_args[0][0]
        assert channel == "onyx:cache_invalidate"

    @pytest.mark.asyncio
    async def test_broadcast_faiss_rebuild(
        self, manager: GatewayInstanceManager, mock_redis: AsyncMock
    ) -> None:
        await manager.broadcast_faiss_rebuild()
        mock_redis.publish.assert_called_once()
        channel = mock_redis.publish.call_args[0][0]
        assert channel == "onyx:faiss_rebuild"

    @pytest.mark.asyncio
    async def test_broadcast_without_redis_is_noop(self) -> None:
        mgr = GatewayInstanceManager()
        # Should not raise
        await mgr.broadcast_cache_invalidate(["k"])
        await mgr.broadcast_faiss_rebuild()


class TestCacheInvalidationInstances:
    @pytest.mark.asyncio
    async def test_two_instances_apply_source_generation_invalidation(self) -> None:
        l1_b = AsyncMock()
        l1_b.stats = {"size": 0}
        l2_b = AsyncMock()
        publisher_redis = AsyncMock()

        instance_a = CacheInvalidator(redis_client=publisher_redis, instance_id="gw-a")
        instance_b = CacheInvalidator(l1=l1_b, l2=l2_b, instance_id="gw-b")
        await instance_b.record_dependency("cache-key-1", source_id="src1", tables=["users"])

        token = await instance_a.prepare_source_write("src1")
        await instance_a.publish_invalidation(
            "src1",
            ["users"],
            cache_keys=["cache-key-1"],
            source_generation=token.generation,
        )

        payload = json.loads(publisher_redis.publish.await_args.args[1])
        count = await instance_b.apply_remote_invalidation(payload)

        l1_b.invalidate.assert_awaited_once_with("cache-key-1")
        l2_b.invalidate.assert_awaited_once_with("cache-key-1")
        assert count == 2
        assert await instance_b.source_generation("src1") == token.generation
