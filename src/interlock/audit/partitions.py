"""Runtime maintenance for monthly audit partitions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

AUDIT_PARTITION_LOCK_KEY = 0x1A0D_1701


@dataclass(frozen=True, slots=True)
class AuditPartitionHealth:
    running: bool
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    lock_skips: int

    @property
    def degraded(self) -> bool:
        return self.last_error is not None or self.consecutive_failures > 0


class AuditPartitionMaintainer:
    """Periodically creates audit partitions under a cluster-wide lock."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        interval_seconds: float = 6 * 60 * 60,
        months_back: int = 1,
        months_forward: int = 3,
    ) -> None:
        self._pool = pool
        self._interval = max(interval_seconds, 1.0)
        self._months_back = max(months_back, 0)
        self._months_forward = max(months_forward, 1)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_run_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._lock_skips = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="audit-partition-maintainer")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("audit partition maintenance failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    async def run_once(self) -> bool:
        """Run maintenance if this instance obtains the advisory lock."""
        self._last_run_at = datetime.now(UTC)
        async with self._pool.acquire() as conn:
            locked = bool(
                await conn.fetchval("SELECT pg_try_advisory_lock($1)", AUDIT_PARTITION_LOCK_KEY)
            )
            if not locked:
                self._lock_skips += 1
                return False
            try:
                await conn.fetchval(
                    "SELECT maintain_audit_partitions($1, $2)",
                    self._months_back,
                    self._months_forward,
                )
                row = await conn.fetchrow("""
                    SELECT last_run_at, last_success_at, last_error
                    FROM audit_partition_maintenance
                    WHERE id = TRUE
                    """)
                self._last_success_at = _row_value(row, "last_success_at") or datetime.now(UTC)
                self._last_error = _row_value(row, "last_error")
                self._consecutive_failures = 0
                return True
            except Exception as exc:
                self._last_error = _safe_error(exc)
                self._consecutive_failures += 1
                raise
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", AUDIT_PARTITION_LOCK_KEY)

    def health(self) -> AuditPartitionHealth:
        return AuditPartitionHealth(
            running=self._task is not None and not self._task.done(),
            last_run_at=self._last_run_at,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
            lock_skips=self._lock_skips,
        )


def _row_value(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, TypeError):
        return None


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"
