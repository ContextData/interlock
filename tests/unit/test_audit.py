"""Tests for AuditLogger."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import asyncpg
import pytest

from interlock.audit.logger import AuditLogger
from interlock.models import AuditRecord


def _make_record(**overrides) -> AuditRecord:
    defaults = {
        "identity_id": 42,
        "source_id": "src-1",
        "operation": "read",
        "sql_fingerprint": "SELECT $1",
        "status": "success",
    }
    defaults.update(overrides)
    return AuditRecord(**defaults)


@pytest.fixture
def mock_pool() -> AsyncMock:
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    return pool


@pytest.mark.asyncio
async def test_log_inserts_record(mock_pool: AsyncMock) -> None:
    logger = AuditLogger(mock_pool)
    record = _make_record()

    await logger.log(record)

    mock_pool.execute.assert_called_once()
    args = mock_pool.execute.call_args
    # First positional arg is the SQL, rest are the params
    sql = args[0][0]
    assert "INSERT INTO audit_log" in sql
    # Check key values are passed through
    params = args[0][1:]
    assert params[0]  # stable event_id
    assert params[1] == 42  # identity_id
    assert params[2] == "src-1"  # source_id
    assert params[3] == "read"  # operation
    assert params[13]["event_id"] == str(params[0])


@pytest.mark.asyncio
async def test_log_keeps_metadata_as_queryable_json_object(mock_pool: AsyncMock) -> None:
    logger = AuditLogger(mock_pool)
    record = _make_record(
        request_metadata={
            "source_role_decision": {"allowed": False},
            "permission_request": {"action": "http.get"},
        }
    )

    await logger.log(record)

    params = mock_pool.execute.call_args[0][1:]
    assert params[13]["source_role_decision"]["allowed"] is False
    assert params[13]["permission_request"]["action"] == "http.get"
    assert params[13]["event_id"] == str(params[0])


@pytest.mark.asyncio
async def test_log_emits_safe_structured_correlation_fields(
    mock_pool: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = AuditLogger(mock_pool)
    record = _make_record(
        protocol="http",
        route="/v1/customers",
        request_metadata={
            "correlation_id": "corr-123",
            "authorization": "Bearer secret-token",
            "body": {"api_key": "secret"},
        },
    )
    caplog.set_level(logging.INFO, logger="interlock.audit.logger")

    await logger.log(record)

    persisted = [item for item in caplog.records if item.message == "audit event persisted"]
    assert persisted
    event = persisted[-1]
    assert event.audit_correlation_id == "corr-123"
    assert event.audit_source_id == "src-1"
    assert event.audit_protocol == "http"
    assert event.audit_status == "success"
    assert "secret-token" not in event.getMessage()
    assert "secret" not in event.getMessage()


@pytest.mark.asyncio
async def test_log_uses_provided_created_at(mock_pool: AsyncMock) -> None:
    ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
    record = _make_record(created_at=ts)

    logger = AuditLogger(mock_pool)
    await logger.log(record)

    # created_at remains at the legacy parameter position even after
    # canonical audit columns are appended.
    params = mock_pool.execute.call_args[0][1:]
    assert params[14] == ts


@pytest.mark.asyncio
async def test_log_reuses_supplied_event_id(mock_pool: AsyncMock) -> None:
    logger = AuditLogger(mock_pool)
    record = _make_record(request_metadata={"event_id": "5f78d048-70ae-4f70-b4e4-aab723fcfd6e"})

    await logger.log(record)

    params = mock_pool.execute.call_args[0][1:]
    assert str(params[0]) == "5f78d048-70ae-4f70-b4e4-aab723fcfd6e"


@pytest.mark.asyncio
async def test_log_handles_pg_error(mock_pool: AsyncMock) -> None:
    mock_pool.execute.side_effect = asyncpg.PostgresError("disk full")

    logger = AuditLogger(mock_pool)
    # Should not raise
    await logger.log(_make_record())


@pytest.mark.asyncio
async def test_log_handles_unexpected_error(mock_pool: AsyncMock) -> None:
    mock_pool.execute.side_effect = RuntimeError("connection lost")

    logger = AuditLogger(mock_pool)
    # Should not raise
    await logger.log(_make_record())
