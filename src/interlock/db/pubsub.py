"""PG NOTIFY + Redis pub/sub manager for the Gateway.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

import asyncpg
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Default PG NOTIFY channels used by InterLock.
DEFAULT_PG_CHANNELS: tuple[str, ...] = (
    "onyx_config_changed",
    "onyx_policy_changed",
    "onyx_write_approval",
)

# Default Redis pub/sub channels used by InterLock.
DEFAULT_REDIS_CHANNELS: tuple[str, ...] = (
    "onyx:faiss_rebuild",
    "onyx:cache_invalidate",
)

# Callback type: receives the channel name and the payload string.
PubSubCallback = Callable[[str, str], Coroutine[Any, Any, None]]


class PubSubManager:
    """Manages PG NOTIFY listeners and Redis pub/sub subscriptions for the Gateway.

    Usage::

        mgr = PubSubManager(pg_pool, redis_client)
        mgr.on_pg_notify("onyx_config_changed", my_reload_handler)
        mgr.on_redis_message("onyx:faiss_rebuild", my_rebuild_handler)
        await mgr.start()
        # ... run forever ...
        await mgr.stop()
    """

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        redis_client: aioredis.Redis,
    ) -> None:
        self._pg_pool = pg_pool
        self._redis = redis_client
        self._pg_connection: asyncpg.Connection | None = None
        self._redis_pubsub: aioredis.client.PubSub | None = None
        self._pg_callbacks: dict[str, list[PubSubCallback]] = defaultdict(list)
        self._redis_callbacks: dict[str, list[PubSubCallback]] = defaultdict(list)
        self._redis_listener_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on_pg_notify(self, channel: str, callback: PubSubCallback) -> None:
        """Register a callback for a PG NOTIFY channel.

        Must be called *before* :meth:`start`.
        """
        self._pg_callbacks[channel].append(callback)

    def on_redis_message(self, channel: str, callback: PubSubCallback) -> None:
        """Register a callback for a Redis pub/sub channel.

        Must be called *before* :meth:`start`.
        """
        self._redis_callbacks[channel].append(callback)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start PG NOTIFY and Redis pub/sub listeners."""
        await self._start_pg_listeners()
        await self._start_redis_listener()
        logger.info("PubSubManager started")

    async def stop(self) -> None:
        """Stop all listeners and close connections."""
        # Stop Redis listener task
        if self._redis_listener_task is not None:
            self._redis_listener_task.cancel()
            try:
                await self._redis_listener_task
            except asyncio.CancelledError:
                pass
            self._redis_listener_task = None

        # Unsubscribe and close Redis pub/sub
        if self._redis_pubsub is not None:
            await self._redis_pubsub.unsubscribe()
            await self._redis_pubsub.aclose()
            self._redis_pubsub = None

        # Remove PG listeners and release connection
        if self._pg_connection is not None:
            for channel in self._pg_callbacks:
                try:
                    await self._pg_connection.remove_listener(channel, self._on_pg_notify)
                except Exception:
                    pass
            await self._pg_connection.close()
            self._pg_connection = None

        logger.info("PubSubManager stopped")

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish_redis(self, channel: str, message: str) -> None:
        """Publish a message to a Redis channel."""
        await self._redis.publish(channel, message.encode())

    # ------------------------------------------------------------------
    # Internal - PG
    # ------------------------------------------------------------------

    async def _start_pg_listeners(self) -> None:
        if not self._pg_callbacks:
            return
        self._pg_connection = await self._pg_pool.acquire()
        for channel in self._pg_callbacks:
            await self._pg_connection.add_listener(channel, self._on_pg_notify)
            logger.info("PG NOTIFY listener registered: %s", channel)

    def _on_pg_notify(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """Synchronous callback required by asyncpg - dispatches to async handlers."""
        callbacks = self._pg_callbacks.get(channel, [])
        for cb in callbacks:
            asyncio.ensure_future(self._safe_call(cb, channel, payload))

    # ------------------------------------------------------------------
    # Internal - Redis
    # ------------------------------------------------------------------

    async def _start_redis_listener(self) -> None:
        if not self._redis_callbacks:
            return
        self._redis_pubsub = self._redis.pubsub()
        await self._redis_pubsub.subscribe(*self._redis_callbacks.keys())
        self._redis_listener_task = asyncio.create_task(self._redis_listen_loop())

    async def _redis_listen_loop(self) -> None:
        """Read messages from Redis pub/sub and dispatch to callbacks."""
        assert self._redis_pubsub is not None
        while True:
            try:
                message = await self._redis_pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is not None and message["type"] == "message":
                    channel = message["channel"]
                    # Channel may be bytes or str depending on decode_responses
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    callbacks = self._redis_callbacks.get(channel, [])
                    for cb in callbacks:
                        await self._safe_call(cb, channel, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in Redis pub/sub listener")
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Internal - shared
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe_call(callback: PubSubCallback, channel: str, payload: str) -> None:
        try:
            await callback(channel, payload)
        except Exception:
            logger.exception("PubSub callback error on channel %s", channel)
