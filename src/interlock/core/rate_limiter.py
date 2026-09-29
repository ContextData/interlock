"""Redis-backed sliding window rate limiter using Lua script for atomicity."""

from __future__ import annotations

import logging
import time
import uuid

import redis.asyncio

from interlock.models import RateLimitResult

logger = logging.getLogger(__name__)

# Lua script: sliding window rate limit check-and-add.
# KEYS[1] = sorted set key
# ARGV[1] = now (epoch seconds as float)
# ARGV[2] = window size in seconds
# ARGV[3] = max requests allowed in window
# ARGV[4] = unique member id for this request
#
# Returns {1, remaining} on allow, {0, 0} on deny.
_LUA_SCRIPT = """\
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, window)
    return {1, limit - count - 1}
else
    return {0, 0}
end
"""


class RateLimiter:
    """Sliding-window rate limiter backed by Redis sorted sets.

    Each request is stored as a member in a sorted set with its timestamp as
    the score. Old entries outside the window are pruned atomically via Lua.
    """

    def __init__(self, redis_client: redis.asyncio.Redis) -> None:
        self._redis = redis_client
        self._script: redis.asyncio.client.Script | None = None

    async def initialize(self) -> None:
        """Register the Lua script with Redis."""
        self._script = self._redis.register_script(_LUA_SCRIPT)

    async def check(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        """Check whether a request is within rate limit.

        Uses sliding window: ZADD timestamp, ZREMRANGEBYSCORE to prune old
        entries, ZCARD to count.

        Returns a RateLimitResult with allowed, remaining, and reset_at.
        """
        now = time.time()
        member = f"{now}:{uuid.uuid4().hex[:8]}"

        try:
            if self._script is None:
                await self.initialize()

            result = await self._script(  # type: ignore[misc]
                keys=[key],
                args=[now, window_seconds, limit, member],
            )

            allowed = bool(result[0])
            remaining = int(result[1])

            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                limit=limit,
                reset_at=now + window_seconds,
            )
        except redis.RedisError:
            # Best-effort: on Redis failure, allow the request through.
            logger.warning("Rate limiter Redis error - allowing request", exc_info=True)
            return RateLimitResult(
                allowed=True,
                remaining=limit,
                limit=limit,
                reset_at=now + window_seconds,
            )

    async def check_multi(
        self,
        identity_id: int,
        source_id: str | None = None,
        session_id: str | None = None,
        limits: dict[str, tuple[int, int]] | None = None,
    ) -> RateLimitResult:
        """Check multiple rate limit dimensions and return the most restrictive.

        Default dimensions checked:
          - per-user: ratelimit:user:{identity_id}
          - per-source (if source_id): ratelimit:source:{source_id}:user:{identity_id}
          - per-session (if session_id): ratelimit:session:{session_id}

        Args:
            identity_id: The identity/user id.
            source_id: Optional data source id for per-source limiting.
            session_id: Optional session id for per-session limiting.
            limits: Optional override mapping dimension key to (limit, window_seconds).
                    If not provided, defaults are used.

        Returns:
            The most restrictive RateLimitResult across all dimensions.
        """
        default_limits: dict[str, tuple[int, int]] = {
            f"ratelimit:user:{identity_id}": (100, 60),
        }
        if source_id is not None:
            default_limits[f"ratelimit:source:{source_id}:user:{identity_id}"] = (50, 60)
        if session_id is not None:
            default_limits[f"ratelimit:session:{session_id}"] = (200, 60)

        checks = limits if limits is not None else default_limits

        most_restrictive: RateLimitResult | None = None

        for key, (limit, window) in checks.items():
            result = await self.check(key, limit, window)
            result.dimension = key

            if most_restrictive is None:
                most_restrictive = result
            elif not result.allowed:
                # Denied always wins
                most_restrictive = result
            elif most_restrictive.allowed and result.remaining < most_restrictive.remaining:
                # Both allowed: pick the one with fewer remaining
                most_restrictive = result

        # Should never be None since checks is non-empty, but guard anyway.
        if most_restrictive is None:
            now = time.time()
            return RateLimitResult(
                allowed=True,
                remaining=0,
                limit=0,
                reset_at=now,
            )

        return most_restrictive
