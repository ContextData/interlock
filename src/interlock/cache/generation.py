"""Cache generation and source-write barrier helpers."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from interlock.errors import CacheBarrierUnavailableError

SOURCE_GENERATION_KEY_PREFIX = "onyx:cache:source_generation:"


@dataclass(frozen=True)
class SourceGenerationToken:
    """A source generation observed or advanced before a write."""

    source_id: str
    generation: int


class SourceGenerationBarrier:
    """Small Redis-backed generation primitive for source writes.

    Callers can advance a source generation before a write, then use the
    returned token as a barrier for cache readers or follow-up invalidation.
    The in-process fallback keeps tests and single-process deployments useful
    when Redis is not configured.
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        *,
        key_prefix: str = SOURCE_GENERATION_KEY_PREFIX,
    ) -> None:
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._local_generations: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def for_prefix(cls, redis_client: Any | None, prefix: str) -> SourceGenerationBarrier:
        """The barrier every process must share for `cache.source_generation_prefix`.

        The gateway and the admin both build one. They once used different
        prefixes, so admin invalidations advanced a counter no gateway read.
        """
        return cls(redis_client, key_prefix=prefix.rstrip(":") + ":")

    async def current(self, source_id: str) -> int:
        """Return the latest known source generation."""
        if self._redis is not None:
            try:
                raw = await self._redis.get(self._key(source_id))
                if raw is None:
                    return 0
                if isinstance(raw, bytes):
                    raw = raw.decode("ascii")
                return int(raw)
            except Exception:
                pass
        return self._local_generations.get(source_id, 0)

    async def advance(self, source_id: str) -> SourceGenerationToken:
        """Increment and return a source generation token."""
        if self._redis is not None:
            try:
                generation = int(await self._redis.incr(self._key(source_id)))
                self._local_generations[source_id] = max(
                    generation,
                    self._local_generations.get(source_id, 0),
                )
                return SourceGenerationToken(source_id=source_id, generation=generation)
            except Exception:
                pass

        async with self._lock:
            generation = self._local_generations.get(source_id, 0) + 1
            self._local_generations[source_id] = generation
            return SourceGenerationToken(source_id=source_id, generation=generation)

    async def advance_shared(self, source_id: str) -> SourceGenerationToken:
        """Advance Redis generation without a process-local fallback."""
        if self._redis is None:
            raise CacheBarrierUnavailableError("Cache write barrier unavailable")
        try:
            generation = int(await self._redis.incr(self._key(source_id)))
        except Exception as exc:
            raise CacheBarrierUnavailableError("Cache write barrier unavailable") from exc
        token = SourceGenerationToken(source_id=source_id, generation=generation)
        await self.mark_seen(token)
        return token

    async def current_shared(self, source_id: str) -> int:
        """Read Redis generation without accepting a stale local fallback."""
        if self._redis is None:
            raise CacheBarrierUnavailableError("Cache write barrier unavailable")
        try:
            raw = await self._redis.get(self._key(source_id))
            if raw is None:
                return 0
            if isinstance(raw, bytes):
                raw = raw.decode("ascii")
            return int(raw)
        except Exception as exc:
            raise CacheBarrierUnavailableError("Cache write barrier unavailable") from exc

    async def mark_seen(self, token: SourceGenerationToken) -> None:
        """Record a generation seen from another instance."""
        async with self._lock:
            self._local_generations[token.source_id] = max(
                token.generation,
                self._local_generations.get(token.source_id, 0),
            )

    async def wait_until_at_least(
        self,
        token: SourceGenerationToken,
        *,
        timeout_seconds: float = 1.0,
        poll_interval_seconds: float = 0.01,
    ) -> bool:
        """Wait until the source generation reaches the token generation."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            if await self.current(token.source_id) >= token.generation:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll_interval_seconds)

    def _key(self, source_id: str) -> str:
        return f"{self._key_prefix}{source_id}"
