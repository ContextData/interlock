"""Audit logger.

AUDIT-COVERS: P1-E (synchronous insert -> async buffered)

Two modes:

- ``AuditLogger(pool, buffer=None)`` performs a direct synchronous
  INSERT (legacy path; preserved so unit tests with mocked pools keep
  working).
- ``AuditLogger(pool, buffer=AuditBuffer(pool))`` enqueues to the
  buffer for batched COPY writes. This is the production path; the
  gateway lifespan wires this up.
"""

from __future__ import annotations

import logging

import asyncpg

from interlock.audit.buffer import _INSERT_SQL, AuditBuffer, _event_to_row, make_audit_event
from interlock.audit.structured import log_audit_persisted
from interlock.models import AuditRecord

logger = logging.getLogger(__name__)


class AuditLogger:
    """Writes audit rows to PG, optionally via a background batched buffer.

    The audit_log table is partitioned - we just INSERT and PG routes
    to the correct partition automatically.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        buffer: AuditBuffer | None = None,
    ) -> None:
        self._pool = pool
        self._buffer = buffer

    @property
    def buffer(self) -> AuditBuffer | None:
        return self._buffer

    async def log(self, record: AuditRecord) -> None:
        """Persist a single audit record."""
        if self._buffer is not None:
            await self._buffer.log(record)
            return

        event = make_audit_event(record)
        row = _event_to_row(event)
        try:
            await self._pool.execute(_INSERT_SQL, *row)
            log_audit_persisted(
                logger,
                event.record,
                metadata=event.record.request_metadata,
            )
        except asyncpg.PostgresError as exc:
            logger.warning("Audit INSERT failed: %s", exc)
        except Exception as exc:
            logger.warning("Unexpected audit error: %s", exc)
