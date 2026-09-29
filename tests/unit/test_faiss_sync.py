"""Unit tests for FAISSIndexSync."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.cache.faiss_sync import FAISS_REBUILD_CHANNEL, FAISSIndexSync


def _make_mock_index():
    index = MagicMock()
    index.rebuild = AsyncMock()
    return index


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


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        cache = _make_mock_index()
        discovery = _make_mock_index()
        sync = FAISSIndexSync(cache, discovery, rebuild_interval_seconds=1.0)

        await sync.start()
        assert sync._running is True
        assert sync._task is not None

        await sync.stop()
        assert sync._running is False
        assert sync._task is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        cache = _make_mock_index()
        discovery = _make_mock_index()
        sync = FAISSIndexSync(cache, discovery, rebuild_interval_seconds=100.0)

        await sync.start()
        task1 = sync._task
        await sync.start()  # second call should be no-op
        assert sync._task is task1

        await sync.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        cache = _make_mock_index()
        discovery = _make_mock_index()
        sync = FAISSIndexSync(cache, discovery)

        # Should not raise
        await sync.stop()
        assert sync._running is False


# ---------------------------------------------------------------------------
# rebuild_now
# ---------------------------------------------------------------------------


class TestRebuildNow:
    @pytest.mark.asyncio
    async def test_rebuilds_both_indexes(self):
        cache = _make_mock_index()
        discovery = _make_mock_index()
        sync = FAISSIndexSync(cache, discovery)

        await sync.rebuild_now()

        cache.rebuild.assert_awaited_once()
        discovery.rebuild.assert_awaited_once()


# ---------------------------------------------------------------------------
# on_pubsub_rebuild
# ---------------------------------------------------------------------------


class TestOnPubsubRebuild:
    @pytest.mark.asyncio
    async def test_triggers_rebuild(self):
        cache = _make_mock_index()
        discovery = _make_mock_index()
        sync = FAISSIndexSync(cache, discovery)

        await sync.on_pubsub_rebuild("force_rebuild")

        cache.rebuild.assert_awaited_once()
        discovery.rebuild.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_with_different_messages(self):
        cache = _make_mock_index()
        discovery = _make_mock_index()
        sync = FAISSIndexSync(cache, discovery)

        await sync.on_pubsub_rebuild("index_updated")
        await sync.on_pubsub_rebuild("manual_trigger")

        assert cache.rebuild.await_count == 2
        assert discovery.rebuild.await_count == 2

    @pytest.mark.asyncio
    async def test_generation_message_reloads_matching_namespace(self):
        cache = _make_mock_index()
        cache.namespace = "cache"
        cache.redis_generation_gap.return_value = 1
        cache.reload_if_redis_generation_changed = AsyncMock(return_value=True)
        discovery = _make_mock_index()
        discovery.namespace = "discovery"
        sync = FAISSIndexSync(cache, discovery)

        await sync.on_pubsub_rebuild(json.dumps({"namespace": "cache", "generation": 2}))

        cache.redis_generation_gap.assert_called_once_with(2)
        cache.reload_if_redis_generation_changed.assert_awaited_once_with()
        cache.rebuild.assert_not_awaited()
        discovery.rebuild.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generation_gap_forces_full_resync(self):
        cache = _make_mock_index()
        cache.namespace = "cache"
        cache.redis_generation = 1
        cache.redis_generation_gap.return_value = 4
        cache.reload_if_redis_generation_changed = AsyncMock(return_value=True)
        discovery = _make_mock_index()
        discovery.namespace = "discovery"
        sync = FAISSIndexSync(cache, discovery)

        await sync.on_pubsub_rebuild(json.dumps({"namespace": "cache", "generation": 5}))

        cache.reload_if_redis_generation_changed.assert_awaited_once_with(force=True)
        cache.rebuild.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pubsub_listener_reconnects_after_error(self):
        first = FakePubSub(fail_once=True)
        second = FakePubSub(
            [
                {
                    "data": json.dumps({"namespace": "cache", "generation": 1}),
                }
            ]
        )
        redis = FakeRedis([first, second])
        cache = _make_mock_index()
        cache.namespace = "cache"
        cache.redis_generation_gap.return_value = 1
        cache.reload_if_redis_generation_changed = AsyncMock(return_value=True)
        discovery = _make_mock_index()
        discovery.namespace = "discovery"
        sync = FAISSIndexSync(
            cache,
            discovery,
            redis_client=redis,
            reconnect_backoff_seconds=0,
        )

        await sync.start()
        try:
            for _ in range(50):
                if redis.created >= 2 and cache.reload_if_redis_generation_changed.await_count:
                    break
                await asyncio.sleep(0.01)
        finally:
            await sync.stop()

        assert redis.created >= 2
        first.unsubscribe.assert_awaited_once_with(FAISS_REBUILD_CHANNEL)
        first.aclose.assert_awaited_once()
        cache.reload_if_redis_generation_changed.assert_awaited_once_with()
