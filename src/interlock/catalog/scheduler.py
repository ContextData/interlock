"""Scheduled refresh of the source catalog, and retention of its history.

Every worker ticks. Each tick takes a transaction-scoped advisory lock so that
normally only one worker does the work, but correctness does not depend on the
lock: the partial unique index on pending scans makes enqueueing idempotent, so
two workers ticking together still queue one scan per source.

A source is due when it has no scan waiting or running and either it has never
been scanned, its last successful scan is older than its refresh interval, or
its last attempt failed and the retry interval has passed. A source's
`metadata.catalog_refresh_interval_seconds` overrides the default, and 0 turns
scheduled refresh off for that source - useful for a Snowflake warehouse whose
scans cost credits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from interlock.catalog.collectors import has_collector
from interlock.catalog.queue import enqueue_catalog_scan
from interlock.config import CatalogConfig

logger = logging.getLogger(__name__)

# An arbitrary constant naming the scheduler's advisory lock.
SCHEDULER_LOCK_KEY = 7_431_902_116

PER_SOURCE_INTERVAL_KEY = "catalog_refresh_interval_seconds"

_STATE_SQL = """
SELECT ds.source_id,
       ds.source_type,
       ds.metadata,
       (SELECT MAX(s.finished_at) FROM source_catalog_scans s
         WHERE s.source_id = ds.source_id AND s.status = 'succeeded') AS last_success,
       (SELECT MAX(s.finished_at) FROM source_catalog_scans s
         WHERE s.source_id = ds.source_id AND s.status = 'failed') AS last_failure,
       EXISTS (SELECT 1 FROM source_catalog_scans s
                WHERE s.source_id = ds.source_id
                  AND s.status IN ('pending', 'running')) AS busy
FROM data_sources ds
WHERE ds.enabled
ORDER BY ds.source_id
"""

_PRUNE_SCANS_SQL = """
DELETE FROM source_catalog_scans
WHERE status IN ('succeeded', 'failed', 'cancelled')
  AND id IN (
      SELECT id FROM (
          SELECT id, ROW_NUMBER() OVER (
              PARTITION BY source_id ORDER BY requested_at DESC, id DESC
          ) AS rank
          FROM source_catalog_scans
      ) ranked
      WHERE ranked.rank > $1
  )
"""

_PRUNE_CHANGES_SQL = """
DELETE FROM source_catalog_changes
WHERE created_at < NOW() - make_interval(days => $1)
"""


@dataclass(frozen=True, slots=True)
class SourceScanState:
    source_id: str
    connector_key: str
    metadata: dict[str, Any] = field(default_factory=dict)
    last_success: datetime | None = None
    last_failure: datetime | None = None
    busy: bool = False


def _interval_for(state: SourceScanState, default_interval: int) -> int | None:
    """The source's refresh interval in seconds, or None when it is off."""
    raw = state.metadata.get(PER_SOURCE_INTERVAL_KEY, default_interval)
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        interval = default_interval
    return None if interval <= 0 else interval


def due_sources(
    states: list[SourceScanState],
    *,
    now: datetime,
    default_interval: int,
    failure_retry: int,
) -> list[str]:
    """The sources a scheduler tick should queue a scan for."""
    due: list[str] = []
    for state in states:
        if state.busy or not has_collector(state.connector_key):
            continue
        interval = _interval_for(state, default_interval)
        if interval is None:
            continue
        if state.last_success is None and state.last_failure is None:
            due.append(state.source_id)
            continue
        last_failure, last_success = state.last_failure, state.last_success
        if last_failure is not None and (last_success is None or last_failure > last_success):
            # The most recent attempt failed: retry on the shorter failure clock.
            if now - last_failure >= timedelta(seconds=failure_retry):
                due.append(state.source_id)
        elif last_success is not None and now - last_success >= timedelta(seconds=interval):
            due.append(state.source_id)
    return due


def _state_from_row(row: Any) -> SourceScanState:
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    if not isinstance(metadata, dict):
        metadata = {}
    return SourceScanState(
        source_id=str(row["source_id"]),
        connector_key=str(
            metadata.get("connector_key") or metadata.get("provider") or row["source_type"]
        ),
        metadata=metadata,
        last_success=row["last_success"],
        last_failure=row["last_failure"],
        busy=bool(row["busy"]),
    )


class CatalogScheduler:
    """Queues due scans and trims history, once per tick, until stopped."""

    def __init__(self, *, pg_pool: Any, config: CatalogConfig) -> None:
        self._pool = pg_pool
        self._config = config
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="catalog-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Catalog scheduler tick failed", exc_info=True)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._config.scheduler_tick_seconds
                )

    async def tick(self, *, now: datetime | None = None) -> list[int]:
        """Queue scans for every due source and apply retention.

        Returns the ids of the scans queued. Returns nothing when another
        worker holds the scheduler lock this tick.
        """
        queued: list[int] = []
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if not await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1)", SCHEDULER_LOCK_KEY
                ):
                    return queued
                rows = await conn.fetch(_STATE_SQL)
                current = now or await conn.fetchval("SELECT NOW()")
                due = due_sources(
                    [_state_from_row(row) for row in rows],
                    now=current,
                    default_interval=self._config.refresh_interval_seconds,
                    failure_retry=self._config.failure_retry_seconds,
                )
                for source_id in due:
                    scan_id = await enqueue_catalog_scan(
                        conn,
                        source_id,
                        trigger="scheduled",
                        delay_seconds=random.uniform(0, self._config.scheduler_jitter_seconds),
                    )
                    if scan_id is not None:
                        queued.append(scan_id)
                await conn.execute(_PRUNE_SCANS_SQL, self._config.scan_history_retention)
                await conn.execute(_PRUNE_CHANGES_SQL, self._config.change_retention_days)
        if queued:
            logger.info("Catalog scheduler queued %d scan(s)", len(queued))
        return queued
