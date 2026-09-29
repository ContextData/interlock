"""PG NOTIFY listener helper."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# Type alias for async notification callbacks.
NotifyCallback = Callable[[asyncpg.Connection, int, str, str], Coroutine[Any, Any, None]]

# Default channels used by InterLock for real-time config/policy propagation.
DEFAULT_CHANNELS: tuple[str, ...] = (
    "onyx_config_changed",
    "onyx_policy_changed",
    "onyx_write_approval",
)


async def setup_pg_listeners(
    pool: asyncpg.Pool,
    channels: dict[str, NotifyCallback],
) -> asyncpg.Connection:
    """Subscribe to PG NOTIFY channels with async callbacks.

    Acquires a dedicated connection from *pool* and registers listeners
    for each channel in *channels*.  The caller is responsible for
    keeping the returned connection alive (and eventually releasing it).

    Args:
        pool: An asyncpg connection pool.
        channels: Mapping of channel name to an async callback that
            receives (connection, pid, channel, payload).

    Returns:
        The dedicated connection used for listening.  Hold a reference
        to it for the lifetime of the subscription.
    """
    conn: asyncpg.Connection = await pool.acquire()
    for channel, callback in channels.items():
        await conn.add_listener(channel, callback)  # type: ignore[arg-type]
        logger.info("Subscribed to PG NOTIFY channel: %s", channel)
    return conn
