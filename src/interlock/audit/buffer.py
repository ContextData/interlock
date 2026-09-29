"""Loss-intolerant buffered audit delivery.

Audit events receive a stable UUID before entering the queue.  Database
delivery is retried with bounded exponential backoff; unavailable batches are
written to a fsync-backed spool and replayed later.  Invalid individual events
are isolated in a database dead-letter table with a file fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from interlock.audit.partitions import AuditPartitionHealth, AuditPartitionMaintainer
from interlock.audit.structured import log_audit_persisted
from interlock.errors import AuditUnavailableError
from interlock.models import AuditRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: UUID
    record: AuditRecord


@dataclass(frozen=True, slots=True)
class AuditHealth:
    running: bool
    queue_depth: int
    queue_capacity: int
    memory_backlog: int
    spool_pending: int
    dlq_count: int
    written: int
    failed: int
    replayed: int
    overflow_spooled: int
    last_success_at: datetime | None
    last_error: str | None
    partition: AuditPartitionHealth | None

    @property
    def degraded(self) -> bool:
        """Whether audit delivery is failing *now*.

        Deliberately excludes `dlq_count`. A dead letter is a historical
        durability incident - an event that could not be persisted and was set
        aside - and it deserves an alert. It does not mean the gateway cannot
        serve, and treating it as a readiness failure was a self-inflicted
        outage: `_dlq_count` is only ever incremented, and is re-read from the
        dead-letter file at construction, so a single poison event marked the
        gateway permanently unready *across restarts*. In Kubernetes that
        removes a healthy pod from service and never puts it back.

        Demonstrated: one line appended to the dead-letter file made an
        otherwise perfectly healthy gateway answer 503 on /ready forever,
        including after successful traffic.

        The count stays visible in the readiness payload so it can be alerted
        on. The fail-closed guarantee is unaffected - in strict mode a request
        that cannot be audited is still refused, which is a separate mechanism
        from readiness.
        """
        partition_degraded = self.partition is not None and self.partition.degraded
        return bool(
            self.memory_backlog or self.spool_pending or self.last_error or partition_degraded
        )

    @property
    def has_dead_letters(self) -> bool:
        """Events set aside as unpersistable. Alert on it; do not gate traffic on it."""
        return bool(self.dlq_count)


class AuditBuffer:
    """Background audit writer with durable overflow and failure recovery."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        max_size: int = 50_000,
        flush_interval_ms: int = 100,
        flush_batch_size: int = 1_000,
        retry_attempts: int = 3,
        retry_base_seconds: float = 0.05,
        retry_max_seconds: float = 1.0,
        spool_dir: str | Path | None = None,
        durability_mode: str = "retriable",
        partition_maintenance_enabled: bool = True,
        partition_interval_seconds: float = 6 * 60 * 60,
        partition_months_back: int = 1,
        partition_months_forward: int = 3,
    ) -> None:
        self._pool = pool
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=max_size)
        self._flush_interval = max(flush_interval_ms / 1000.0, 0.001)
        self._flush_batch = max(flush_batch_size, 1)
        self._retry_attempts = max(retry_attempts, 1)
        self._retry_base = max(retry_base_seconds, 0.0)
        self._retry_max = max(retry_max_seconds, self._retry_base)
        if durability_mode not in {"best_effort", "retriable", "strict"}:
            raise ValueError("invalid audit durability mode")
        self._durability_mode = durability_mode
        configured_spool = os.environ.get("INTERLOCK_AUDIT_SPOOL_DIR")
        state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        default_spool = (
            Path(configured_spool) if configured_spool else state_root / "interlock" / "audit-spool"
        )
        self._spool_dir = Path(spool_dir or default_spool)
        self._spool_path = self._spool_dir / "audit-spool.jsonl"
        self._dlq_path = self._spool_dir / "audit-dead-letter.jsonl"
        self._spool_lock = asyncio.Lock()
        self._memory_backlog: list[AuditEvent] = []
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._partition_maintainer = (
            AuditPartitionMaintainer(
                pool,
                interval_seconds=partition_interval_seconds,
                months_back=partition_months_back,
                months_forward=partition_months_forward,
            )
            if partition_maintenance_enabled
            else None
        )

        self.dropped = 0  # Compatibility metric: durable mode never intentionally drops.
        self.written = 0
        self.failed = 0
        self.replayed = 0
        self.dead_lettered = 0
        self.overflow_spooled = 0
        self._spool_pending = _count_lines(self._spool_path)
        self._dlq_count = _count_lines(self._dlq_path)
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        try:
            await asyncio.to_thread(_verify_spool_directory, self._spool_dir)
        except OSError as exc:
            self._last_error = _safe_error(exc)
            if self._durability_mode == "strict":
                raise AuditUnavailableError(
                    "strict audit delivery requires a writable durable spool"
                ) from exc
            logger.error("audit spool is not writable: %s", type(exc).__name__)
        self._stopping.clear()
        if self._partition_maintainer is not None:
            await self._partition_maintainer.start()
        self._task = asyncio.create_task(self._run(), name="audit-flusher")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._stopping.set()
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

        deadline = monotonic() + 10.0
        while (self._memory_backlog or not self._queue.empty()) and monotonic() < deadline:
            await self._flush_now()
        if self._memory_backlog or not self._queue.empty():
            remaining = self._memory_backlog + self._drain_queue(self._queue.qsize())
            self._memory_backlog = []
            try:
                await self._spool_events(remaining)
            except Exception as exc:
                self._memory_backlog = remaining
                self._last_error = _safe_error(exc)
                raise AuditUnavailableError(
                    f"shutdown could not durably preserve {len(remaining)} audit events"
                ) from exc
            logger.error(
                "audit shutdown deadline reached; spooled %d queued events",
                len(remaining),
            )
        if self._partition_maintainer is not None:
            await self._partition_maintainer.stop()

    async def log(self, record: AuditRecord) -> None:
        """Queue an event, durably spooling it when the queue is saturated."""
        event = make_audit_event(record)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            if self._durability_mode == "best_effort":
                self.dropped += 1
                self._last_error = "audit queue full; event dropped in best_effort mode"
                return
            try:
                await self._spool_events([event])
            except Exception as exc:
                self._last_error = _safe_error(exc)
                if self._durability_mode == "retriable":
                    self._memory_backlog.append(event)
                    return
                raise AuditUnavailableError(
                    "audit event could not be queued or durably spooled"
                ) from exc
            self.overflow_spooled += 1
            if self.overflow_spooled == 1 or self.overflow_spooled % 1000 == 0:
                logger.warning(
                    "audit queue full; durably spooled %d events",
                    self.overflow_spooled,
                )

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._replay_spool_once()
                await self._flush_now()
            except Exception as exc:
                self._last_error = _safe_error(exc)
                logger.exception("audit delivery loop iteration failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._flush_interval)
            except TimeoutError:
                continue

    async def _flush_now(self) -> None:
        if self._memory_backlog:
            batch = self._memory_backlog[: self._flush_batch]
            del self._memory_backlog[: self._flush_batch]
        else:
            batch = self._drain_queue(self._flush_batch)
        if not batch:
            return
        await self._deliver_or_recover(batch)

    def _drain_queue(self, limit: int) -> list[AuditEvent]:
        batch: list[AuditEvent] = []
        while len(batch) < limit:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _deliver_or_recover(self, batch: list[AuditEvent]) -> None:
        try:
            await self._persist_with_retry(batch)
        except Exception as exc:
            self.failed += len(batch)
            self._last_error = _safe_error(exc)
            if _is_poison_error(exc):
                await self._isolate_poison_batch(batch)
            else:
                try:
                    await self._spool_events(batch)
                except Exception as spool_exc:
                    self._last_error = _safe_error(spool_exc)
                    self._memory_backlog = batch + self._memory_backlog
                    logger.exception(
                        "audit database and spool unavailable; retaining %d events in memory",
                        len(batch),
                    )
                else:
                    logger.error(
                        "audit batch unavailable after %d attempts; spooled %d events",
                        self._retry_attempts,
                        len(batch),
                    )

    async def _persist_with_retry(self, batch: list[AuditEvent]) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                await self._write_batch(batch)
                self.written += len(batch)
                self._last_success_at = datetime.now(UTC)
                self._last_error = None
                for event in batch:
                    log_audit_persisted(
                        logger,
                        event.record,
                        metadata=_metadata_object(event.record.request_metadata),
                    )
                return
            except Exception as exc:
                last_error = exc
                if _is_poison_error(exc) or attempt == self._retry_attempts:
                    break
                delay = min(self._retry_base * (2 ** (attempt - 1)), self._retry_max)
                if delay:
                    await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def _write_batch(self, batch: list[AuditEvent]) -> None:
        rows = [_event_to_row(event) for event in batch]
        async with self._pool.acquire() as conn:
            await conn.executemany(_INSERT_SQL, rows)

    async def _isolate_poison_batch(self, batch: list[AuditEvent]) -> None:
        for event in batch:
            try:
                await self._write_batch([event])
            except Exception as exc:
                if _is_poison_error(exc):
                    try:
                        await self._dead_letter(event, exc)
                    except Exception as dlq_exc:
                        self._last_error = _safe_error(dlq_exc)
                        self._memory_backlog.append(event)
                        logger.exception(
                            "audit dead-letter sinks unavailable; retaining event in memory"
                        )
                else:
                    try:
                        await self._spool_events([event])
                    except Exception as spool_exc:
                        self._last_error = _safe_error(spool_exc)
                        self._memory_backlog.append(event)
                        logger.exception(
                            "audit spool unavailable during poison isolation; retaining event"
                        )
            else:
                self.written += 1
                self._last_success_at = datetime.now(UTC)
                log_audit_persisted(
                    logger,
                    event.record,
                    metadata=_metadata_object(event.record.request_metadata),
                )

    async def _replay_spool_once(self) -> None:
        # Atomically take ownership of the current spool contents. Producers
        # may append to a fresh spool while database replay is in progress.
        async with self._spool_lock:
            lines = await asyncio.to_thread(_read_lines, self._spool_path)
            if not lines:
                self._spool_pending = 0
                return
            await asyncio.to_thread(_rewrite_lines, self._spool_path, [])

        selected = lines[: self._flush_batch]
        remaining = lines[self._flush_batch :]
        preserved: list[str] = []
        events: list[AuditEvent] = []
        for line in selected:
            try:
                events.append(_event_from_json(line))
            except Exception as exc:
                await self._append_dlq_payload(
                    {
                        "event": {"raw_spool_line": line[:10_000]},
                        "attempts": 0,
                        "error_type": type(exc).__name__,
                        "last_error": _safe_error(exc),
                        "failed_at": datetime.now(UTC).isoformat(),
                    }
                )
                self.dead_lettered += 1

        if events:
            try:
                await self._persist_with_retry(events)
            except Exception as exc:
                if _is_poison_error(exc):
                    await self._isolate_poison_batch(events)
                else:
                    self._last_error = _safe_error(exc)
                    preserved = [_event_to_json(event) for event in events]
            else:
                self.replayed += len(events)

        # Merge untouched and concurrently appended records. No producer data
        # can be overwritten by replay cleanup.
        async with self._spool_lock:
            concurrent = await asyncio.to_thread(_read_lines, self._spool_path)
            merged = preserved + remaining + concurrent
            await asyncio.to_thread(_rewrite_lines, self._spool_path, merged)
            self._spool_pending = len(merged)

    async def _spool_events(self, events: list[AuditEvent]) -> None:
        if not events:
            return
        payload = "".join(_event_to_json(event) + "\n" for event in events)
        async with self._spool_lock:
            await asyncio.to_thread(_append_fsynced, self._spool_path, payload)
            self._spool_pending += len(events)

    async def _dead_letter(self, event: AuditEvent, exc: Exception) -> None:
        payload = _event_payload(event)
        error = _safe_error(exc)
        file_fallback = False
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    _DLQ_INSERT_SQL,
                    event.event_id,
                    payload,
                    self._retry_attempts,
                    type(exc).__name__,
                    error,
                )
        except Exception:
            file_fallback = True
            await self._append_dlq_payload(
                {
                    "event": payload,
                    "attempts": self._retry_attempts,
                    "error_type": type(exc).__name__,
                    "last_error": error,
                    "failed_at": datetime.now(UTC).isoformat(),
                }
            )
        self.dead_lettered += 1
        if not file_fallback:
            self._dlq_count += 1
        self._last_error = f"poison audit event {event.event_id} moved to dead letter"

    async def _append_dlq_payload(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        await asyncio.to_thread(_append_fsynced, self._dlq_path, encoded)
        self._dlq_count += 1

    def health(self) -> AuditHealth:
        partition = (
            self._partition_maintainer.health() if self._partition_maintainer is not None else None
        )
        return AuditHealth(
            running=self._task is not None and not self._task.done(),
            queue_depth=self._queue.qsize(),
            queue_capacity=self._queue.maxsize,
            memory_backlog=len(self._memory_backlog),
            spool_pending=self._spool_pending,
            dlq_count=self._dlq_count,
            written=self.written,
            failed=self.failed,
            replayed=self.replayed,
            overflow_spooled=self.overflow_spooled,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            partition=partition,
        )


_INSERT_SQL = """
WITH claimed AS (
    INSERT INTO audit_event_dedup (event_id, event_created_at)
    VALUES ($1, $15)
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
)
INSERT INTO audit_log (
    event_id, identity_id, source_id, operation, sql_fingerprint,
    cache_hit, cache_tier, latency_ms, pii_detected, pii_types,
    risk_level, status, error_message, request_metadata, created_at,
    protocol, route, normalized_operation, intent, upstream_target,
    policy_decision, approval_id, approval_status, redaction_stats, cost_metadata
)
SELECT
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14::jsonb, $15, $16, $17, $18, $19, $20,
    $21::jsonb, $22, $23, $24::jsonb, $25::jsonb
FROM claimed
"""

_DLQ_INSERT_SQL = """
INSERT INTO audit_dead_letter (
    event_id, payload, attempts, error_type, last_error
)
VALUES ($1, $2::jsonb, $3, $4, $5)
ON CONFLICT (event_id) DO UPDATE SET
    attempts = audit_dead_letter.attempts + EXCLUDED.attempts,
    error_type = EXCLUDED.error_type,
    last_error = EXCLUDED.last_error,
    last_failed_at = NOW()
"""


def make_audit_event(record: AuditRecord) -> AuditEvent:
    metadata = _metadata_object(record.request_metadata)
    supplied = metadata.get("event_id")
    try:
        event_id = UUID(str(supplied)) if supplied else uuid4()
    except (TypeError, ValueError, AttributeError):
        event_id = uuid4()
    metadata["event_id"] = str(event_id)
    return AuditEvent(
        event_id=event_id,
        record=record.model_copy(
            update={
                "request_metadata": metadata,
                "created_at": record.created_at or datetime.now(UTC),
            }
        ),
    )


def _event_to_row(event: AuditEvent) -> tuple[Any, ...]:
    record = event.record
    created = record.created_at or datetime.now(UTC)
    metadata = _metadata_object(record.request_metadata)
    return (
        event.event_id,
        record.identity_id,
        record.source_id,
        record.operation,
        record.sql_fingerprint,
        record.cache_hit,
        record.cache_tier,
        record.latency_ms,
        record.pii_detected,
        record.pii_types,
        record.risk_level,
        record.status,
        record.error_message,
        metadata,
        created,
        record.protocol or metadata.get("protocol"),
        record.route or metadata.get("route") or metadata.get("path"),
        record.normalized_operation
        or metadata.get("normalized_operation")
        or metadata.get("operation"),
        record.intent or metadata.get("intent"),
        record.upstream_target or metadata.get("upstream_target") or metadata.get("upstream_url"),
        record.policy_decision or metadata.get("policy_decision"),
        record.approval_id or metadata.get("approval_id"),
        record.approval_status or metadata.get("approval_status") or metadata.get("approval_state"),
        record.redaction_stats or metadata.get("redaction_stats"),
        record.cost_metadata or metadata.get("cost_metadata"),
    )


def _event_payload(event: AuditEvent) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "record": event.record.model_dump(mode="json"),
    }


def _event_to_json(event: AuditEvent) -> str:
    return json.dumps(_event_payload(event), separators=(",", ":"), sort_keys=True)


def _event_from_json(raw: str) -> AuditEvent:
    payload = json.loads(raw)
    return AuditEvent(
        event_id=UUID(str(payload["event_id"])),
        record=AuditRecord.model_validate(payload["record"]),
    )


def _metadata_object(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"raw": raw}
    return {"value": raw}


def _is_poison_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            asyncpg.DataError,
            asyncpg.IntegrityConstraintViolationError,
        ),
    )


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def _append_fsynced(path: Path, payload: str) -> None:
    _ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _rewrite_lines(path: Path, lines: list[str]) -> None:
    if not path.exists() and not lines:
        return
    _ensure_private_directory(path.parent)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        os.chmod(path, 0o700)


def _verify_spool_directory(path: Path) -> None:
    """Prove the configured spool can durably accept an audit event."""
    _ensure_private_directory(path)
    probe = path / ".write-probe"
    descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("audit-spool-ready\n")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        probe.unlink(missing_ok=True)


def _count_lines(path: Path) -> int:
    try:
        return len(_read_lines(path))
    except OSError:
        return 0
