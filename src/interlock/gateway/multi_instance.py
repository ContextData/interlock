"""Multi-instance gateway coordination via Redis."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from uuid import uuid4

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Redis key prefix for gateway instance registration.
INSTANCE_KEY_PREFIX = "onyx:gateway:instance:"

# Redis channel for cache invalidation across instances.
CACHE_INVALIDATE_CHANNEL = "onyx:cache_invalidate"

# Redis channel for FAISS rebuild signals.
FAISS_REBUILD_CHANNEL = "onyx:faiss_rebuild"

# How long (seconds) before an instance is considered stale.
STALE_THRESHOLD_MULTIPLIER = 3.0


class GatewayInstanceManager:
    """Manages gateway instance registration and coordination.

    Each gateway process registers itself in Redis with a heartbeat.
    Other instances can discover peers via :meth:`get_active_instances`.
    Stale entries (missed heartbeats) are automatically expired via
    Redis key TTL.
    """

    def __init__(
        self,
        instance_id: str | None = None,
        redis_client: aioredis.Redis | None = None,
        heartbeat_interval: float = 10.0,
    ) -> None:
        self._instance_id = instance_id or f"gw-{uuid4().hex[:8]}"
        self._redis = redis_client
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_task: asyncio.Task | None = None
        self._started = False
        # TTL for the Redis key - allows automatic cleanup of dead instances.
        self._key_ttl = int(heartbeat_interval * STALE_THRESHOLD_MULTIPLIER) + 1

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        """Return the unique identifier for this gateway instance."""
        return self._instance_id

    @property
    def started(self) -> bool:
        """Return whether the instance manager is running."""
        return self._started

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Register this instance in Redis and start the heartbeat loop."""
        if self._started:
            return
        if self._redis is None:
            logger.warning("No Redis client provided - multi-instance coordination disabled")
            return

        self._started = True
        await self._register()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(), name=f"heartbeat-{self._instance_id}"
        )
        logger.info(
            "Gateway instance %s registered (heartbeat every %.0fs)",
            self._instance_id,
            self._heartbeat_interval,
        )

    async def stop(self) -> None:
        """Deregister this instance and stop the heartbeat loop."""
        self._started = False

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._redis is not None:
            await self._deregister()

        logger.info("Gateway instance %s deregistered", self._instance_id)

    # ------------------------------------------------------------------
    # Instance discovery
    # ------------------------------------------------------------------

    async def get_active_instances(self) -> list[dict]:
        """Return metadata for all active gateway instances.

        Each entry is a dict with keys: ``instance_id``, ``started_at``,
        ``last_heartbeat``.
        """
        if self._redis is None:
            return []

        pattern = f"{INSTANCE_KEY_PREFIX}*"
        instances: list[dict] = []
        async for key in self._redis.scan_iter(match=pattern, count=100):
            raw = await self._redis.get(key)
            if raw is not None:
                try:
                    data = json.loads(raw)
                    instances.append(data)
                except (json.JSONDecodeError, TypeError):
                    continue
        return instances

    # ------------------------------------------------------------------
    # Cache invalidation broadcast
    # ------------------------------------------------------------------

    async def broadcast_cache_invalidate(self, keys: list[str]) -> None:
        """Publish a cache invalidation message to all instances."""
        if self._redis is None:
            return
        payload = json.dumps(
            {
                "source_instance": self._instance_id,
                "keys": keys,
                "timestamp": time.time(),
            }
        )
        await self._redis.publish(CACHE_INVALIDATE_CHANNEL, payload)

    async def broadcast_faiss_rebuild(self) -> None:
        """Signal all instances to rebuild their FAISS indexes."""
        if self._redis is None:
            return
        payload = json.dumps(
            {
                "source_instance": self._instance_id,
                "timestamp": time.time(),
            }
        )
        await self._redis.publish(FAISS_REBUILD_CHANNEL, payload)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _register(self) -> None:
        """Write this instance's metadata to Redis."""
        key = f"{INSTANCE_KEY_PREFIX}{self._instance_id}"
        data = json.dumps(
            {
                "instance_id": self._instance_id,
                "started_at": time.time(),
                "last_heartbeat": time.time(),
            }
        )
        await self._redis.set(key, data, ex=self._key_ttl)

    async def _deregister(self) -> None:
        """Remove this instance's metadata from Redis."""
        key = f"{INSTANCE_KEY_PREFIX}{self._instance_id}"
        await self._redis.delete(key)

    async def _heartbeat(self) -> None:
        """Periodically update the instance's heartbeat timestamp in Redis."""
        while self._started:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._started:
                    break
                key = f"{INSTANCE_KEY_PREFIX}{self._instance_id}"
                data = json.dumps(
                    {
                        "instance_id": self._instance_id,
                        "started_at": time.time(),
                        "last_heartbeat": time.time(),
                    }
                )
                await self._redis.set(key, data, ex=self._key_ttl)
                logger.debug("Heartbeat updated for instance %s", self._instance_id)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "Error updating heartbeat for instance %s",
                    self._instance_id,
                )
