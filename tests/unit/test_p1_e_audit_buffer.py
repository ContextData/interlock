"""Regression test for audit P1-E: audit logging is async-buffered.

AUDIT-COVERS: P1-E

The audit reported synchronous per-call INSERTs on the request path.
The fix adds AuditBuffer with asyncio.Queue + COPY-batched writer task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from interlock.audit.buffer import AuditBuffer, make_audit_event
from interlock.audit.logger import AuditLogger
from interlock.audit.partitions import AuditPartitionMaintainer
from interlock.errors import AuditUnavailableError
from interlock.models import AuditRecord


def _make_record(i: int = 0) -> AuditRecord:
    return AuditRecord(
        identity_id=i,
        source_id="src",
        operation="read",
        sql_fingerprint=f"fp-{i}",
        cache_hit=False,
        cache_tier=None,
        latency_ms=1.0,
        pii_detected=False,
        pii_types=[],
        status="success",
        request_metadata={"i": i, "correlation_id": f"corr-{i}"},
    )


@pytest.mark.asyncio
async def test_strict_audit_start_fails_when_spool_is_not_writable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pool, _ = _make_pool_with_copy()

    def reject_spool(_path: Path) -> None:
        raise OSError("read-only audit volume")

    monkeypatch.setattr("interlock.audit.buffer._verify_spool_directory", reject_spool)
    buf = AuditBuffer(pool, spool_dir=tmp_path, durability_mode="strict")

    with pytest.raises(AuditUnavailableError, match="writable durable spool"):
        await buf.start()


@pytest.mark.asyncio
async def test_retriable_audit_health_degrades_when_spool_is_not_writable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pool, _ = _make_pool_with_copy()

    def reject_spool(_path: Path) -> None:
        raise OSError("read-only audit volume")

    monkeypatch.setattr("interlock.audit.buffer._verify_spool_directory", reject_spool)
    buf = AuditBuffer(pool, spool_dir=tmp_path, durability_mode="retriable")
    await buf.start()
    try:
        assert buf.health().degraded is True
        assert "OSError" in (buf.health().last_error or "")
    finally:
        await buf.shutdown()


def _make_pool_with_copy() -> tuple[MagicMock, MagicMock]:
    """Return (pool, conn) where conn.executemany is an AsyncMock."""
    conn = MagicMock()
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=False)
    conn.fetchrow = AsyncMock(return_value=None)

    class _Acquire:
        async def __aenter__(_self):
            return conn

        async def __aexit__(_self, *exc):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool, conn


@pytest.mark.asyncio
async def test_p1_e_log_returns_immediately_without_executing() -> None:
    """log() should never call pool.execute on the buffered path."""
    pool, conn = _make_pool_with_copy()
    pool.execute = AsyncMock()
    buf = AuditBuffer(pool, max_size=100, flush_interval_ms=5_000)
    await buf.start()
    try:
        # Enqueue several records very quickly.
        for i in range(5):
            await buf.log(_make_record(i))
        # No flush yet - execute and copy were not called.
        pool.execute.assert_not_called()
        conn.executemany.assert_not_called()
    finally:
        await buf.shutdown()


@pytest.mark.asyncio
async def test_p1_e_records_are_batched_via_copy() -> None:
    pool, conn = _make_pool_with_copy()
    buf = AuditBuffer(pool, max_size=100, flush_interval_ms=20)
    await buf.start()
    try:
        for i in range(10):
            await buf.log(_make_record(i))
        # Wait for the periodic flush.
        await asyncio.sleep(0.1)
    finally:
        await buf.shutdown()
    # COPY was called at least once with 10 records in one or more batches.
    assert conn.executemany.await_count >= 1
    total = sum(
        # executemany(sql, rows) - rows is the second positional arg.
        len(call.args[1])
        for call in conn.executemany.await_args_list
    )
    assert total == 10


@pytest.mark.asyncio
async def test_p1_e_buffer_emits_structured_correlation_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool, _conn = _make_pool_with_copy()
    buf = AuditBuffer(pool, max_size=100, flush_interval_ms=10_000)
    caplog.set_level(logging.INFO, logger="interlock.audit.buffer")

    await buf.log(_make_record(9))
    await buf._flush_now()

    persisted = [item for item in caplog.records if item.message == "audit event persisted"]
    assert persisted
    assert persisted[-1].audit_correlation_id == "corr-9"
    assert persisted[-1].audit_source_id == "src"


@pytest.mark.asyncio
async def test_p1_e_queue_full_spools_without_dropping(tmp_path: Path) -> None:
    pool, conn = _make_pool_with_copy()
    buf = AuditBuffer(
        pool,
        max_size=2,
        flush_interval_ms=10_000,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )
    # Don't start the flusher - we want the queue to fill up.
    for i in range(5):
        await buf.log(_make_record(i))

    assert buf.dropped == 0
    assert buf._queue.qsize() == 2
    assert buf.health().spool_pending == 3
    lines = (tmp_path / "audit-spool.jsonl").read_text().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line)["event_id"] for line in lines)
    assert stat.S_IMODE((tmp_path / "audit-spool.jsonl").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_p1_e_shutdown_drains_queue() -> None:
    pool, conn = _make_pool_with_copy()
    buf = AuditBuffer(pool, max_size=100, flush_interval_ms=10_000)
    await buf.start()
    for i in range(7):
        await buf.log(_make_record(i))
    # Trigger shutdown before the periodic flush would fire.
    await buf.shutdown()
    assert conn.executemany.await_count >= 1
    total = sum(
        # executemany(sql, rows) - rows is the second positional arg.
        len(call.args[1])
        for call in conn.executemany.await_args_list
    )
    assert total == 7


@pytest.mark.asyncio
async def test_p1_e_shutdown_drains_more_than_one_batch() -> None:
    pool, conn = _make_pool_with_copy()
    buf = AuditBuffer(pool, max_size=100, flush_interval_ms=10_000, flush_batch_size=3)
    await buf.start()
    for i in range(8):
        await buf.log(_make_record(i))

    await buf.shutdown()

    assert conn.executemany.await_count == 3
    total = sum(len(call.args[1]) for call in conn.executemany.await_args_list)
    assert total == 8


@pytest.mark.asyncio
async def test_p1_e_failed_batch_retries_then_spools(tmp_path: Path) -> None:
    pool, conn = _make_pool_with_copy()
    conn.executemany = AsyncMock(side_effect=Exception("db unavailable"))
    buf = AuditBuffer(
        pool,
        max_size=100,
        flush_interval_ms=10_000,
        retry_attempts=3,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )

    await buf.log(_make_record(1))
    await buf._flush_now()

    assert buf.failed == 1
    assert conn.executemany.await_count == 3
    assert buf.health().spool_pending == 1
    assert buf.health().degraded is True


@pytest.mark.asyncio
async def test_p1_e_spooled_event_replays_after_restart(tmp_path: Path) -> None:
    failing_pool, failing_conn = _make_pool_with_copy()
    failing_conn.executemany = AsyncMock(side_effect=Exception("db unavailable"))
    first = AuditBuffer(
        failing_pool,
        retry_attempts=1,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )
    await first.log(_make_record(7))
    await first._flush_now()
    assert first.health().spool_pending == 1

    healthy_pool, healthy_conn = _make_pool_with_copy()
    second = AuditBuffer(
        healthy_pool,
        retry_attempts=1,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )
    await second._replay_spool_once()

    assert healthy_conn.executemany.await_count == 1
    assert second.health().spool_pending == 0
    assert second.replayed == 1


@pytest.mark.asyncio
async def test_p1_e_replay_preserves_concurrent_spool_append(tmp_path: Path) -> None:
    failing_pool, failing_conn = _make_pool_with_copy()
    failing_conn.executemany = AsyncMock(side_effect=Exception("db unavailable"))
    first = AuditBuffer(
        failing_pool,
        retry_attempts=1,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )
    await first.log(_make_record(20))
    await first._flush_now()

    healthy_pool, _healthy_conn = _make_pool_with_copy()
    second = AuditBuffer(
        healthy_pool,
        retry_attempts=1,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )

    async def _persist_while_producer_appends(_batch: object) -> None:
        await second._spool_events([make_audit_event(_make_record(21))])

    second._persist_with_retry = _persist_while_producer_appends  # type: ignore[method-assign]
    await second._replay_spool_once()

    lines = (tmp_path / "audit-spool.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["record"]["identity_id"] == 21
    assert second.health().spool_pending == 1


@pytest.mark.asyncio
async def test_p1_e_poison_event_goes_to_dead_letter(tmp_path: Path) -> None:
    pool, conn = _make_pool_with_copy()
    conn.executemany = AsyncMock(side_effect=asyncpg.DataError("invalid input for audit row"))
    conn.execute = AsyncMock(side_effect=Exception("dead-letter table unavailable"))
    buf = AuditBuffer(
        pool,
        retry_attempts=1,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )

    await buf.log(_make_record(3))
    await buf._flush_now()

    assert buf.dead_lettered == 1
    assert buf.health().dlq_count == 1
    payload = json.loads((tmp_path / "audit-dead-letter.jsonl").read_text())
    assert payload["event"]["event_id"]
    assert payload["error_type"] == "DataError"


@pytest.mark.asyncio
async def test_p1_e_poison_isolation_preserves_valid_batch_records(tmp_path: Path) -> None:
    pool, conn = _make_pool_with_copy()
    conn.executemany = AsyncMock(
        side_effect=[
            asyncpg.DataError("batch contains invalid row"),
            None,
            asyncpg.DataError("invalid row"),
        ]
    )
    conn.execute = AsyncMock()
    buf = AuditBuffer(
        pool,
        retry_attempts=1,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )

    await buf.log(_make_record(30))
    await buf.log(_make_record(31))
    await buf._flush_now()

    assert buf.written == 1
    assert buf.dead_lettered == 1
    assert conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_p1_e_database_and_spool_failure_retains_memory_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, conn = _make_pool_with_copy()
    conn.executemany = AsyncMock(side_effect=Exception("db unavailable"))

    def _spool_unavailable(_path: Path, _payload: str) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr("interlock.audit.buffer._append_fsynced", _spool_unavailable)
    buf = AuditBuffer(
        pool,
        retry_attempts=1,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )

    await buf.log(_make_record(32))
    await buf._flush_now()

    assert buf.health().memory_backlog == 1
    assert buf.health().degraded is True


@pytest.mark.asyncio
async def test_p1_e_strict_mode_rejects_when_queue_and_spool_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, _conn = _make_pool_with_copy()

    def _spool_unavailable(_path: Path, _payload: str) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr("interlock.audit.buffer._append_fsynced", _spool_unavailable)
    buf = AuditBuffer(
        pool,
        max_size=1,
        spool_dir=tmp_path,
        durability_mode="strict",
        partition_maintenance_enabled=False,
    )
    await buf.log(_make_record(40))

    with pytest.raises(AuditUnavailableError):
        await buf.log(_make_record(41))

    assert buf.health().queue_depth == 1
    assert buf.health().degraded is True


@pytest.mark.asyncio
async def test_p1_e_event_id_is_stable_across_retry(tmp_path: Path) -> None:
    pool, conn = _make_pool_with_copy()
    conn.executemany = AsyncMock(side_effect=[Exception("temporary"), None])
    buf = AuditBuffer(
        pool,
        retry_attempts=2,
        retry_base_seconds=0,
        spool_dir=tmp_path,
        partition_maintenance_enabled=False,
    )

    await buf.log(_make_record(11))
    await buf._flush_now()

    first_rows = conn.executemany.await_args_list[0].args[1]
    second_rows = conn.executemany.await_args_list[1].args[1]
    assert first_rows[0][0] == second_rows[0][0]
    assert first_rows[0][14] == second_rows[0][14]
    assert buf.written == 1


@pytest.mark.asyncio
async def test_partition_maintainer_uses_advisory_lock() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[True, None])
    conn.fetchrow = AsyncMock(
        return_value={
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
        }
    )

    class _Acquire:
        async def __aenter__(_self):
            return conn

        async def __aexit__(_self, *exc):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    maintainer = AuditPartitionMaintainer(pool)

    ran = await maintainer.run_once()

    assert ran is True
    sql_calls = [call.args[0] for call in conn.fetchval.await_args_list]
    assert any("pg_try_advisory_lock" in sql for sql in sql_calls)
    assert any("maintain_audit_partitions" in sql for sql in sql_calls)
    conn.execute.assert_awaited_once()
    assert "pg_advisory_unlock" in conn.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_partition_maintainer_skips_when_lock_is_held() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=False)

    class _Acquire:
        async def __aenter__(_self):
            return conn

        async def __aexit__(_self, *exc):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    maintainer = AuditPartitionMaintainer(pool)

    ran = await maintainer.run_once()

    assert ran is False
    assert maintainer.health().lock_skips == 1


@pytest.mark.asyncio
async def test_partition_maintainer_releases_lock_and_degrades_on_failure() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[True, RuntimeError("partition failure")])

    class _Acquire:
        async def __aenter__(_self):
            return conn

        async def __aexit__(_self, *exc):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    maintainer = AuditPartitionMaintainer(pool)

    with pytest.raises(RuntimeError, match="partition failure"):
        await maintainer.run_once()

    conn.execute.assert_awaited_once()
    assert "pg_advisory_unlock" in conn.execute.await_args.args[0]
    assert maintainer.health().degraded is True
    assert maintainer.health().consecutive_failures == 1


@pytest.mark.asyncio
async def test_p1_e_audit_logger_uses_buffer_when_provided() -> None:
    pool, conn = _make_pool_with_copy()
    pool.execute = AsyncMock()
    buf = AuditBuffer(pool, max_size=10, flush_interval_ms=10_000)
    await buf.start()
    try:
        log = AuditLogger(pool, buffer=buf)
        await log.log(_make_record())
        # The synchronous insert path must NOT have run.
        pool.execute.assert_not_called()
        # The record sits in the buffer queue.
        assert buf._queue.qsize() == 1
    finally:
        await buf.shutdown()


@pytest.mark.asyncio
async def test_p1_e_audit_logger_falls_back_to_sync_without_buffer() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock()
    log = AuditLogger(pool, buffer=None)
    await log.log(_make_record())
    pool.execute.assert_awaited_once()
