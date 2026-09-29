"""L2 Redis-backed deterministic cache with msgpack serialization."""

from __future__ import annotations

import logging
import time
from typing import Any

import msgpack
import redis.asyncio as aioredis

from interlock.models import CacheResult

logger = logging.getLogger(__name__)


class L2Cache:
    """Async Redis-backed cache layer.

    All Redis errors are caught and logged - the cache is best-effort and
    must never cause a request to fail.  Values are stored as msgpack
    payloads containing ``{"data": bytes, "metadata": dict, "stored_at": float}``.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        ttl_seconds: int = 300,
        key_prefix: str = "cache:det:",
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._prefix = key_prefix
        self._hits = 0
        self._misses = 0

    # -- helpers -----------------------------------------------------------

    def _make_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    # -- public async API --------------------------------------------------

    async def get(self, key: str) -> CacheResult:
        try:
            raw = await self._redis.get(self._make_key(key))
        except Exception:
            logger.warning("L2 cache GET failed for key=%s", key, exc_info=True)
            self._misses += 1
            return CacheResult(hit=False)

        if raw is None:
            self._misses += 1
            return CacheResult(hit=False)

        try:
            payload: dict[str, Any] = msgpack.unpackb(raw, raw=False)
            self._hits += 1
            return CacheResult(
                hit=True,
                data=payload["data"],
                tier="l2",
                metadata=payload.get("metadata") or {},
            )
        except Exception:
            logger.warning("L2 cache deserialization failed for key=%s", key, exc_info=True)
            self._misses += 1
            return CacheResult(hit=False)

    async def put(
        self,
        key: str,
        data: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = msgpack.packb(
            {
                "data": data,
                "metadata": metadata or {},
                "stored_at": time.time(),
            },
            use_bin_type=True,
        )
        try:
            await self._redis.set(self._make_key(key), payload, ex=self._ttl)
        except Exception:
            logger.warning("L2 cache PUT failed for key=%s", key, exc_info=True)

    async def invalidate(self, key: str) -> None:
        try:
            await self._redis.delete(self._make_key(key))
        except Exception:
            logger.warning("L2 cache DEL failed for key=%s", key, exc_info=True)

    async def clear(self) -> int:
        """Delete all deterministic cache keys for this prefix.

        This is intentionally bounded by Redis SCAN rather than KEYS so write
        invalidation can clear L2 without blocking Redis on large deployments.
        Returns the number of keys deleted on a best-effort basis.
        """
        deleted = 0
        try:
            cursor = b"0"
            while True:
                cursor, keys = await self._redis.scan(
                    cursor=cursor,
                    match=f"{self._prefix}*",
                    count=500,
                )
                if keys:
                    deleted += await self._redis.delete(*keys)
                if cursor == b"0" or cursor == 0:
                    break
        except Exception:
            logger.warning("L2 cache CLEAR failed", exc_info=True)
        return deleted

    # -- stats -------------------------------------------------------------

    @property
    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
        }
