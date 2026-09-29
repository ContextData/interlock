"""L1 in-process deterministic cache backed by cachetools.TTLCache."""

from __future__ import annotations

import threading

from cachetools import TTLCache

from interlock.models import CacheResult


class L1Cache:
    """Async-compatible in-process LRU+TTL cache.

    Wraps ``cachetools.TTLCache`` with an async interface so call sites
    can treat L1 and L2 uniformly.  All operations are O(1) and protected
    by a threading lock for safety when used from multiple coroutines on
    the same event loop thread (or from background threads).
    """

    def __init__(self, max_size: int = 10000, ttl_seconds: int = 60) -> None:
        self._cache: TTLCache[str, bytes] = TTLCache(maxsize=max_size, ttl=ttl_seconds)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # -- public async API --------------------------------------------------

    async def get(self, key: str) -> CacheResult:
        with self._lock:
            try:
                data = self._cache[key]
                self._hits += 1
                return CacheResult(hit=True, data=data, tier="l1")
            except KeyError:
                self._misses += 1
                return CacheResult(hit=False)

    async def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._cache[key] = data

    async def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    async def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    # -- stats -------------------------------------------------------------

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._cache),
                "max_size": self._cache.maxsize,
            }
