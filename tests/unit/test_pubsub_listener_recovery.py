"""Redis pub/sub listeners survive a Redis restart.

On 2026-10-02 the managed Valkey behind the DigitalOcean rehearsal restarted.
Both listeners caught the dropped connection and went to clean up, but the
cleanup sent UNSUBSCRIBE over the dead connection, that raised too, and the
exception escaped the loop. The tasks ended, `/ready` reported the listeners
unhealthy, and the gateway and admin served 503 for about 41 hours until the
pods were restarted by hand.

These tests drive each listener through the same sequence: the connection
drops, cleanup fails because Redis is still down, a reconnect fails while it
is down, then Redis is back and a message arrives. The listener must still be
running and must handle that message.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from interlock.cache.faiss_sync import FAISSIndexSync
from interlock.cache.invalidation import CacheInvalidator


class _DeadPubSub:
    """A subscription whose connection has gone, on a server that is down."""

    def __init__(self, *, subscribe_fails: bool = False) -> None:
        self._subscribe_fails = subscribe_fails

    async def subscribe(self, *channels: str) -> None:
        if self._subscribe_fails:
            raise RedisConnectionError("Error connecting: Connection refused.")

    async def get_message(self, **_: Any) -> dict[str, Any] | None:
        raise RedisConnectionError("Connection closed by server.")

    async def unsubscribe(self, *channels: str) -> None:
        raise RedisConnectionError("Connection closed by server.")

    async def aclose(self) -> None:
        raise RedisConnectionError("Connection closed by server.")


class _LivePubSub:
    """A healthy subscription that delivers one message, then idles."""

    def __init__(self, data: str) -> None:
        self._pending = [{"type": "message", "data": data.encode("utf-8")}]

    async def subscribe(self, *channels: str) -> None:
        return None

    async def get_message(self, **_: Any) -> dict[str, Any] | None:
        if self._pending:
            return self._pending.pop(0)
        await asyncio.sleep(0.01)
        return None

    async def unsubscribe(self, *channels: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _redis_restarting(message: str) -> MagicMock:
    """Drop the live connection, fail one reconnect, then come back."""
    redis = MagicMock()
    redis.pubsub.side_effect = [
        _DeadPubSub(),
        _DeadPubSub(subscribe_fails=True),
        _LivePubSub(message),
    ]
    return redis


async def _until(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_cache_invalidation_listener_survives_a_redis_restart() -> None:
    payload = {"source_id": "sales_pg", "source_instance": "peer"}
    redis = _redis_restarting(json.dumps(payload))
    invalidator = CacheInvalidator(redis_client=redis, reconnect_backoff_seconds=0.01)
    invalidator.apply_remote_invalidation = AsyncMock(return_value=0)  # type: ignore[method-assign]

    await invalidator.start_listener()
    try:
        handled = await _until(lambda: invalidator.apply_remote_invalidation.await_count == 1)
        assert handled, "the listener stopped instead of reconnecting"
        assert invalidator.listener_healthy
        invalidator.apply_remote_invalidation.assert_awaited_with(payload)
    finally:
        await invalidator.stop_listener()


@pytest.mark.asyncio
async def test_faiss_sync_listener_survives_a_redis_restart() -> None:
    redis = _redis_restarting('{"namespace": "cache"}')
    sync = FAISSIndexSync(
        cache_index=MagicMock(),
        discovery_index=MagicMock(),
        rebuild_interval_seconds=3600,
        redis_client=redis,
        reconnect_backoff_seconds=0.01,
    )
    sync.on_pubsub_rebuild = AsyncMock()  # type: ignore[method-assign]

    await sync.start()
    try:
        handled = await _until(lambda: sync.on_pubsub_rebuild.await_count == 1)
        assert handled, "the listener stopped instead of reconnecting"
        assert sync.healthy
    finally:
        await sync.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["invalidation", "faiss"])
async def test_stopping_does_not_raise_when_redis_is_down(which: str) -> None:
    """Shutdown during an outage must not crash the lifespan either."""
    redis = MagicMock()
    redis.pubsub.side_effect = lambda: _DeadPubSub()
    if which == "invalidation":
        invalidator = CacheInvalidator(redis_client=redis, reconnect_backoff_seconds=0.01)
        await invalidator.start_listener()
        await asyncio.sleep(0.05)
        invalidator._redis_pubsub = _DeadPubSub()
        await invalidator.stop_listener()
    else:
        sync = FAISSIndexSync(
            cache_index=MagicMock(),
            discovery_index=MagicMock(),
            rebuild_interval_seconds=3600,
            redis_client=redis,
            reconnect_backoff_seconds=0.01,
        )
        await sync.start()
        await asyncio.sleep(0.05)
        sync._redis_pubsub = _DeadPubSub()
        await sync.stop()
