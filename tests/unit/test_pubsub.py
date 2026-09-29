"""Unit tests for PubSubManager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from interlock.db.pubsub import PubSubManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakePGConnection:
    """Minimal asyncpg.Connection stand-in."""

    def __init__(self):
        self.add_listener = AsyncMock()
        self.remove_listener = AsyncMock()
        self.close = AsyncMock()
        self._listeners: dict[str, list] = {}

    async def add_listener_real(self, channel, callback):
        self._listeners.setdefault(channel, []).append(callback)

    def fire_notify(self, channel, payload):
        """Simulate a PG NOTIFY event."""
        for cb in self._listeners.get(channel, []):
            cb(self, 12345, channel, payload)


class FakePGPool:
    def __init__(self, conn: FakePGConnection):
        self._conn = conn

    async def acquire(self):
        return self._conn


class FakeRedisPubSub:
    def __init__(self):
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()
        self.aclose = AsyncMock()
        self._messages: list[dict] = []
        self._call_count = 0

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        if self._messages:
            return self._messages.pop(0)
        # After returning None once, raise CancelledError to stop loop in tests
        self._call_count += 1
        if self._call_count > 2:
            raise asyncio.CancelledError()
        return None


class FakeRedis:
    def __init__(self):
        self.publish = AsyncMock()
        self._pubsub = FakeRedisPubSub()

    def pubsub(self):
        return self._pubsub


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_conn():
    return FakePGConnection()


@pytest.fixture
def pg_pool(pg_conn):
    return FakePGPool(pg_conn)


@pytest.fixture
def redis_client():
    return FakeRedis()


@pytest.fixture
def manager(pg_pool, redis_client):
    return PubSubManager(pg_pool, redis_client)


# ---------------------------------------------------------------------------
# PG NOTIFY tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pg_notify_callback_fires(pg_pool, redis_client, pg_conn):
    """Verify that a registered PG NOTIFY callback is invoked."""
    received = []

    async def handler(channel: str, payload: str) -> None:
        received.append((channel, payload))

    mgr = PubSubManager(pg_pool, redis_client)
    mgr.on_pg_notify("onyx_config_changed", handler)

    # Use add_listener_real so we can fire_notify later
    pg_conn.add_listener = pg_conn.add_listener_real

    await mgr._start_pg_listeners()

    # Simulate a notification
    pg_conn.fire_notify("onyx_config_changed", '{"reload": true}')

    # Give the ensure_future callbacks a chance to run
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0] == ("onyx_config_changed", '{"reload": true}')

    # Cleanup
    mgr._pg_connection = None  # skip remove_listener on fake


@pytest.mark.anyio
async def test_pg_notify_multiple_callbacks(pg_pool, redis_client, pg_conn):
    """Multiple callbacks on the same channel all fire."""
    results_a = []
    results_b = []

    async def handler_a(channel: str, payload: str) -> None:
        results_a.append(payload)

    async def handler_b(channel: str, payload: str) -> None:
        results_b.append(payload)

    mgr = PubSubManager(pg_pool, redis_client)
    mgr.on_pg_notify("onyx_write_approval", handler_a)
    mgr.on_pg_notify("onyx_write_approval", handler_b)

    pg_conn.add_listener = pg_conn.add_listener_real
    await mgr._start_pg_listeners()

    pg_conn.fire_notify("onyx_write_approval", '{"id": 1}')
    await asyncio.sleep(0.05)

    assert len(results_a) == 1
    assert len(results_b) == 1
    mgr._pg_connection = None


# ---------------------------------------------------------------------------
# Redis pub/sub tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_redis_pubsub_callback_fires(pg_pool, redis_client):
    """Verify that a registered Redis callback is invoked on message."""
    received = []

    async def handler(channel: str, payload: str) -> None:
        received.append((channel, payload))

    mgr = PubSubManager(pg_pool, redis_client)
    mgr.on_redis_message("onyx:faiss_rebuild", handler)

    # Pre-load a message into the fake pubsub
    redis_client._pubsub._messages.append(
        {
            "type": "message",
            "channel": b"onyx:faiss_rebuild",
            "data": b'{"index": "main"}',
        }
    )

    await mgr._start_redis_listener()

    # Let the listener loop run
    await asyncio.sleep(0.1)

    # Cancel the listener task
    if mgr._redis_listener_task:
        mgr._redis_listener_task.cancel()
        try:
            await mgr._redis_listener_task
        except asyncio.CancelledError:
            pass

    assert len(received) == 1
    assert received[0] == ("onyx:faiss_rebuild", '{"index": "main"}')


@pytest.mark.anyio
async def test_redis_publish(pg_pool, redis_client):
    """publish_redis sends data to the Redis client."""
    mgr = PubSubManager(pg_pool, redis_client)
    await mgr.publish_redis("onyx:cache_invalidate", '{"key": "abc"}')
    redis_client.publish.assert_awaited_once_with("onyx:cache_invalidate", b'{"key": "abc"}')


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_start_stop_lifecycle(pg_pool, redis_client, pg_conn):
    """start() and stop() complete without errors."""
    mgr = PubSubManager(pg_pool, redis_client)

    async def noop(channel: str, payload: str) -> None:
        pass

    mgr.on_pg_notify("onyx_config_changed", noop)
    mgr.on_redis_message("onyx:faiss_rebuild", noop)

    await mgr.start()

    # Verify PG listener was set up
    pg_conn.add_listener.assert_awaited()

    # Verify Redis subscribe was called
    redis_client._pubsub.subscribe.assert_awaited()

    # Verify listener task is running
    assert mgr._redis_listener_task is not None

    await mgr.stop()

    assert mgr._redis_listener_task is None
    assert mgr._redis_pubsub is None


@pytest.mark.anyio
async def test_start_with_no_callbacks(pg_pool, redis_client, pg_conn):
    """start() with no callbacks registered should be a no-op."""
    mgr = PubSubManager(pg_pool, redis_client)
    await mgr.start()
    assert mgr._pg_connection is None
    assert mgr._redis_pubsub is None
    await mgr.stop()
