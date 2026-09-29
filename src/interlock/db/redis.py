"""Async Redis client factory."""

from __future__ import annotations

import redis.asyncio as aioredis

from interlock.config import RedisConfig


async def create_redis_client(config: RedisConfig) -> aioredis.Redis:
    """Create an async Redis client from config.

    Uses a single connection pool with decode_responses=False
    (binary values - we serialize with msgpack).
    """
    pool = aioredis.ConnectionPool.from_url(
        config.url,
        max_connections=config.max_connections,
        decode_responses=False,
    )
    client: aioredis.Redis = aioredis.Redis(connection_pool=pool)
    return client


async def close_redis_client(client: aioredis.Redis) -> None:
    """Gracefully close the Redis client and its connection pool."""
    await client.aclose()
