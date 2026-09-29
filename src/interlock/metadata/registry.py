"""Metadata registry - in-memory cache of data source configs backed by PG."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import asyncpg

from interlock.models import DataSourceConfig, DataSourceType

logger = logging.getLogger(__name__)

RemovalListener = Callable[[frozenset[str]], Awaitable[None]]


class MetadataRegistry:
    """Maintains an in-memory dict of DataSourceConfig loaded from PG.

    Supports PG NOTIFY-driven reload so config changes propagate without restart.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._sources: dict[str, DataSourceConfig] = {}
        self._listener_conn: asyncpg.Connection | None = None
        self._reload_generation = 0
        self._applied_reload_generation = 0
        self._reload_task: asyncio.Task[None] | None = None
        self._reload_lock = asyncio.Lock()
        self._removal_listeners: list[RemovalListener] = []

    def add_removal_listener(self, listener: RemovalListener) -> None:
        """Call `listener` with the ids of sources a reload no longer loads.

        A disabled or deleted source drops out of the registry, but its pooled
        connections and cached answers outlive it unless something clears
        them. The gateway registers a listener that does.
        """
        self._removal_listeners.append(listener)

    async def load(self) -> None:
        """Load all data sources from PG into memory."""
        rows = await self._pool.fetch(
            "SELECT source_id, name, source_type, connection_config, "
            "cache_strategy, enabled, metadata "
            "FROM data_sources "
            "WHERE enabled = TRUE"
        )
        sources: dict[str, DataSourceConfig] = {}
        for row in rows:
            conn_config = row["connection_config"] or {}
            meta = row["metadata"] or {}
            # asyncpg with JSON codecs returns dicts, but handle strings too
            if isinstance(conn_config, str):
                import json as _json

                conn_config = _json.loads(conn_config)
            if isinstance(meta, str):
                import json as _json

                meta = _json.loads(meta)
            if not row["enabled"]:
                continue
            sources[row["source_id"]] = DataSourceConfig(
                source_id=row["source_id"],
                name=row["name"],
                source_type=DataSourceType(row["source_type"]),
                connection_config=conn_config,
                cache_strategy=row["cache_strategy"],
                enabled=row["enabled"],
                metadata=meta,
            )
        removed = frozenset(self._sources) - frozenset(sources)
        self._sources = sources
        logger.info("Loaded %d data source(s) into registry", len(self._sources))
        if removed:
            await self._notify_removed(removed)

    async def _notify_removed(self, removed: frozenset[str]) -> None:
        for listener in self._removal_listeners:
            try:
                await listener(removed)
            except Exception:
                logger.exception("Registry removal listener failed for %s", sorted(removed))

    def get(self, source_id: str) -> DataSourceConfig | None:
        """Synchronous lookup by source_id. Returns None on miss."""
        return self._sources.get(source_id)

    def get_all(self) -> dict[str, DataSourceConfig]:
        """Return a shallow copy of all loaded sources."""
        return dict(self._sources)

    async def setup_listener(self) -> None:
        """Subscribe to onyx_config_changed and reload on every notification."""
        self._listener_conn = await self._pool.acquire()
        await self._listener_conn.add_listener("onyx_config_changed", self._on_notify)
        logger.info("Registry listening on onyx_config_changed")

    def _on_notify(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """Synchronous callback required by asyncpg - schedules async reload."""
        self._schedule_reload()

    def _schedule_reload(self) -> None:
        self._reload_generation += 1
        if self._reload_task is not None and not self._reload_task.done():
            return
        self._reload_task = asyncio.create_task(self._reload_until_current())
        self._reload_task.add_done_callback(self._log_reload_task_result)

    def _log_reload_task_result(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except Exception:
            logger.exception("Registry reload task failed unexpectedly")

    async def _reload_until_current(self) -> None:
        while self._applied_reload_generation < self._reload_generation:
            target_generation = self._reload_generation
            success = await self._reload()
            if not success:
                return
            self._applied_reload_generation = target_generation

    async def _reload(self) -> bool:
        """Reload all sources from PG (full reload, not incremental)."""
        async with self._reload_lock:
            try:
                await self.load()
                logger.info("Registry reloaded via NOTIFY")
                return True
            except Exception:
                logger.exception("Failed to reload registry on NOTIFY")
                return False
