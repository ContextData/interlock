"""Periodic FAISS index rebuilding from Redis."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from interlock.cache.faiss_index import FAISSIndex

logger = logging.getLogger(__name__)

FAISS_REBUILD_CHANNEL = "onyx:faiss_rebuild"


class FAISSIndexSync:
    """Periodic FAISS index rebuilding from Redis.

    Manages two FAISS indexes (cache and discovery) and periodically
    rebuilds them. Also handles pub/sub triggered rebuilds.
    """

    def __init__(
        self,
        cache_index: FAISSIndex,
        discovery_index: FAISSIndex,
        rebuild_interval_seconds: float = 300.0,
        redis_client: Any | None = None,
        reconnect_backoff_seconds: float = 1.0,
    ) -> None:
        self._cache_index = cache_index
        self._discovery_index = discovery_index
        self._interval = rebuild_interval_seconds
        self._redis = redis_client
        self._reconnect_backoff = reconnect_backoff_seconds
        self._task: asyncio.Task | None = None
        self._pubsub_task: asyncio.Task | None = None
        self._redis_pubsub: Any | None = None
        self._running = False

    async def start(self) -> None:
        """Start periodic rebuild background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._periodic_rebuild())
        if self._redis is not None:
            self._pubsub_task = asyncio.create_task(self._listen_for_rebuilds())
        logger.info("FAISS sync started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        """Stop the background task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._pubsub_task is not None:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass
            self._pubsub_task = None
        await self._close_pubsub()
        logger.info("FAISS sync stopped")

    async def rebuild_now(self) -> None:
        """Trigger immediate rebuild of both indexes."""
        logger.info("Rebuilding both FAISS indexes")
        await asyncio.gather(
            self._cache_index.rebuild(),
            self._discovery_index.rebuild(),
        )
        logger.info("FAISS index rebuild complete")

    @property
    def healthy(self) -> bool:
        periodic_ok = self._running and self._task is not None and not self._task.done()
        pubsub_ok = self._redis is None or (
            self._pubsub_task is not None and not self._pubsub_task.done()
        )
        return periodic_ok and pubsub_ok

    async def on_pubsub_rebuild(self, message: str) -> None:
        """Handle onyx:faiss_rebuild pub/sub message."""
        logger.info("Received FAISS rebuild signal: %s", message)
        payload = _parse_rebuild_message(message)
        if payload is None:
            await self.rebuild_now()
            return

        namespace = payload.get("namespace")
        generation = payload.get("generation")
        if not isinstance(namespace, str) or not isinstance(generation, int):
            await self.rebuild_now()
            return

        matched = False
        for index in (self._cache_index, self._discovery_index):
            if getattr(index, "namespace", None) != namespace:
                continue
            matched = True
            await self._apply_generation_signal(index, generation)
        if not matched:
            logger.debug("Ignoring FAISS rebuild for namespace=%s", namespace)

    async def _periodic_rebuild(self) -> None:
        """Background loop that rebuilds indexes on a fixed interval."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break
                await self.rebuild_now()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error during periodic FAISS rebuild")

    async def _listen_for_rebuilds(self) -> None:
        """Listen for rebuild signals and reconnect on pub/sub errors."""
        while self._running:
            try:
                await self._ensure_pubsub()
                assert self._redis_pubsub is not None
                message = await self._redis_pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if not message:
                    await asyncio.sleep(0)
                    continue
                raw = message.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if isinstance(raw, str):
                    await self.on_pubsub_rebuild(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("FAISS pub/sub listener error; reconnecting", exc_info=True)
                # The connection is gone: UNSUBSCRIBE over it can only fail.
                await self._close_pubsub(unsubscribe=False)
                await asyncio.sleep(self._reconnect_backoff)

    async def _ensure_pubsub(self) -> None:
        if self._redis_pubsub is not None:
            return
        assert self._redis is not None
        self._redis_pubsub = self._redis.pubsub()
        await self._redis_pubsub.subscribe(FAISS_REBUILD_CHANNEL)

    async def _close_pubsub(self, *, unsubscribe: bool = True) -> None:
        """Drop the subscription. Never raises: Redis may be why we are here.

        A cleanup error escaping the listener's error handler ends the listener
        for good, which leaves the pod unready until it is restarted.
        """
        pubsub, self._redis_pubsub = self._redis_pubsub, None
        if pubsub is None:
            return
        if unsubscribe:
            try:
                await pubsub.unsubscribe(FAISS_REBUILD_CHANNEL)
            except Exception:
                logger.debug("FAISS unsubscribe failed; closing anyway", exc_info=True)
        try:
            await pubsub.aclose()
        except Exception:
            logger.debug("FAISS subscription close failed", exc_info=True)

    async def _apply_generation_signal(self, index: FAISSIndex, generation: int) -> None:
        gap = index.redis_generation_gap(generation)
        if gap > 1:
            logger.warning(
                "FAISS generation gap detected namespace=%s loaded=%d incoming=%d; full resync",
                index.namespace,
                index.redis_generation,
                generation,
            )
            if await index.reload_if_redis_generation_changed(force=True):
                return
        elif await index.reload_if_redis_generation_changed():
            return
        await index.rebuild()


def _parse_rebuild_message(message: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
