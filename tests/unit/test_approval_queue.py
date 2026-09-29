"""Tests for ApprovalQueue."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from interlock.core.approval_queue import ApprovalQueue
from interlock.errors import InterLockError, WriteApprovalRequiredError
from interlock.models import RiskLevel


@pytest.fixture(autouse=True)
def _safe_test_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "interlock.security.egress.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )


@pytest.fixture
def mock_pool() -> AsyncMock:
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=7)
    pool.execute = AsyncMock(return_value="UPDATE 1")
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    return pool


@pytest.fixture
def mock_conn_mgr() -> AsyncMock:
    mgr = AsyncMock()
    upstream_pool = AsyncMock()
    upstream_pool.execute = AsyncMock(return_value="INSERT 0 1")
    mgr.get_pool = AsyncMock(return_value=upstream_pool)
    return mgr


@pytest.fixture
def queue(mock_pool: AsyncMock) -> ApprovalQueue:
    return ApprovalQueue(mock_pool, expiry_seconds=900)


@pytest.fixture
def queue_with_conn(mock_pool: AsyncMock, mock_conn_mgr: AsyncMock) -> ApprovalQueue:
    return ApprovalQueue(mock_pool, connection_manager=mock_conn_mgr, expiry_seconds=900)


@pytest.fixture
def mock_registry() -> MagicMock:
    source = MagicMock()
    source.base_url = "https://api.example.com/v1"
    registry = MagicMock()
    registry.get.return_value = source
    return registry


# -- submit --


@pytest.mark.asyncio
async def test_submit_creates_entry_and_raises(queue: ApprovalQueue, mock_pool: AsyncMock) -> None:
    with pytest.raises(WriteApprovalRequiredError) as exc_info:
        await queue.submit(
            identity_id=42,
            source_id="warehouse",
            sql_text="DELETE FROM orders WHERE id = 1",
            risk_level=RiskLevel.HIGH,
            metadata={"reason": "cleanup"},
        )

    assert exc_info.value.approval_id == 7
    assert exc_info.value.risk_level == "high"
    mock_pool.fetchval.assert_called_once()
    call_args = mock_pool.fetchval.call_args[0]
    assert call_args[1] == 42  # identity_id
    assert call_args[2] == "warehouse"  # source_id
    assert "DELETE" in call_args[3]  # sql_text
    assert call_args[4] == "high"  # risk_level


# -- approve --


@pytest.mark.asyncio
async def test_approve_updates_status(queue: ApprovalQueue, mock_pool: AsyncMock) -> None:
    result = await queue.approve(7, approved_by="admin@acme.com")

    assert result["status"] == "approved"
    assert result["executed"] is False
    # First execute call is the UPDATE, second is the NOTIFY
    assert mock_pool.execute.call_count >= 1


@pytest.mark.asyncio
async def test_approve_executes_sql_when_conn_mgr_available(
    queue_with_conn: ApprovalQueue,
    mock_pool: AsyncMock,
    mock_conn_mgr: AsyncMock,
) -> None:
    # fetchrow returns a row so execution is attempted
    mock_pool.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "source_id": "warehouse",
            "sql_text": "DELETE FROM orders WHERE id = 1",
            "identity_id": 42,
            "risk_level": "high",
            "status": "approved",
            "approved_by": "admin@acme.com",
            "executed_at": None,
            "expires_at": datetime(2025, 1, 1, tzinfo=UTC),
            "request_metadata": "{}",
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        }
    )

    result = await queue_with_conn.approve(7, approved_by="admin@acme.com")

    assert result["status"] == "approved"
    assert result["executed"] is True
    mock_conn_mgr.get_pool.assert_called_once_with("warehouse")
    upstream_pool = await mock_conn_mgr.get_pool("warehouse")
    upstream_pool.execute.assert_called_with("DELETE FROM orders WHERE id = 1")


@pytest.mark.asyncio
async def test_expired_approval_cannot_execute(
    queue_with_conn: ApprovalQueue,
    mock_pool: AsyncMock,
    mock_conn_mgr: AsyncMock,
) -> None:
    mock_pool.execute = AsyncMock(side_effect=["UPDATE 0", "UPDATE 1"])

    with pytest.raises(InterLockError, match="cannot be approved"):
        await queue_with_conn.approve(7, approved_by="admin@acme.com")

    mock_conn_mgr.get_pool.assert_not_called()
    assert mock_pool.fetchrow.await_count == 0
    # The sweep issues RETURNING now, so it lands on fetch rather than
    # execute; that a failed approve still triggers it is what matters.
    expire_calls = [
        call for call in mock_pool.fetch.call_args_list if "SET status = 'expired'" in call.args[0]
    ]
    assert expire_calls


@pytest.mark.asyncio
async def test_approved_write_executes_with_original_identity_scope(
    queue_with_conn: ApprovalQueue,
    mock_pool: AsyncMock,
    mock_conn_mgr: AsyncMock,
) -> None:
    mock_pool.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "source_id": "warehouse",
            "sql_text": "UPDATE orders SET status = 'archived' WHERE id = 1",
            "identity_id": 42,
            "risk_level": "medium",
            "status": "approved",
            "approved_by": "admin@acme.com",
            "executed_at": None,
            "expires_at": datetime(2025, 1, 1, tzinfo=UTC),
            "request_metadata": {
                "identity_mapped_pg_role": "source_writer",
                "identity_id": 42,
            },
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        }
    )

    await queue_with_conn.approve(7, approved_by="admin@acme.com")

    mock_conn_mgr.get_pool.assert_called_once_with("warehouse", pg_role="source_writer")


@pytest.mark.asyncio
async def test_approve_executes_http_when_metadata_protocol_http(
    mock_pool: AsyncMock,
    mock_registry: MagicMock,
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(204, request=request))
    client = httpx.AsyncClient(transport=transport)
    mock_pool.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "source_id": "api1",
            "sql_text": "HTTP DELETE api1 users/42",
            "identity_id": 42,
            "risk_level": "high",
            "status": "approved",
            "approved_by": "admin@acme.com",
            "executed_at": None,
            "expires_at": datetime(2025, 1, 1, tzinfo=UTC),
            "request_metadata": {
                "protocol": "http",
                "method": "DELETE",
                "path": "users/42",
                "parameters": {"force": "1"},
                "headers": {"content-type": "application/json"},
                "body": {"encoding": "text", "data": '{"reason":"cleanup"}'},
            },
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        }
    )
    queue = ApprovalQueue(
        mock_pool,
        registry=mock_registry,
        http_client=client,
    )

    result = await queue.approve(7, approved_by="admin@acme.com")

    assert result["executed"] is True
    assert mock_pool.execute.call_count >= 2
    await client.aclose()


@pytest.mark.asyncio
async def test_approve_marks_failed_when_http_execution_fails(
    mock_pool: AsyncMock,
    mock_registry: MagicMock,
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, request=request))
    client = httpx.AsyncClient(transport=transport)
    mock_pool.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "source_id": "api1",
            "sql_text": "HTTP DELETE api1 users/42",
            "identity_id": 42,
            "risk_level": "high",
            "status": "approved",
            "approved_by": "admin@acme.com",
            "executed_at": None,
            "expires_at": datetime(2025, 1, 1, tzinfo=UTC),
            "request_metadata": {
                "protocol": "http",
                "method": "DELETE",
                "path": "users/42",
            },
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        }
    )
    queue = ApprovalQueue(
        mock_pool,
        registry=mock_registry,
        http_client=client,
    )

    with pytest.raises(InterLockError, match="failed during execution"):
        await queue.approve(7, approved_by="admin@acme.com")

    failed_calls = [
        call for call in mock_pool.execute.call_args_list if "SET status = 'failed'" in call.args[0]
    ]
    assert failed_calls
    await client.aclose()


@pytest.mark.asyncio
async def test_approve_non_pending_raises(queue: ApprovalQueue, mock_pool: AsyncMock) -> None:
    mock_pool.execute = AsyncMock(return_value="UPDATE 0")

    with pytest.raises(InterLockError, match="cannot be approved"):
        await queue.approve(99, approved_by="admin@acme.com")


# -- reject --


@pytest.mark.asyncio
async def test_reject_updates_status(queue: ApprovalQueue, mock_pool: AsyncMock) -> None:
    await queue.reject(7, rejected_by="admin@acme.com")

    mock_pool.execute.assert_called_once()
    call_args = mock_pool.execute.call_args[0]
    assert call_args[1] == 7
    assert call_args[2] == "admin@acme.com"


# -- get --


@pytest.mark.asyncio
async def test_get_returns_approval_details(queue: ApprovalQueue, mock_pool: AsyncMock) -> None:
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    mock_pool.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "identity_id": 42,
            "source_id": "warehouse",
            "sql_text": "UPDATE t SET x = 1",
            "risk_level": "medium",
            "status": "pending",
            "approved_by": None,
            "executed_at": None,
            "expires_at": now,
            "request_metadata": "{}",
            "created_at": now,
        }
    )

    result = await queue.get(7)

    assert result is not None
    assert result["id"] == 7
    assert result["source_id"] == "warehouse"
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_get_returns_none_for_missing(queue: ApprovalQueue, mock_pool: AsyncMock) -> None:
    mock_pool.fetchrow = AsyncMock(return_value=None)
    result = await queue.get(999)
    assert result is None


# -- list_pending --


@pytest.mark.asyncio
async def test_list_pending_returns_pending_items(
    queue: ApprovalQueue, mock_pool: AsyncMock
) -> None:
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    mock_pool.fetch = AsyncMock(
        return_value=[
            {
                "id": 7,
                "identity_id": 42,
                "source_id": "warehouse",
                "sql_text": "DELETE FROM t",
                "risk_level": "high",
                "status": "pending",
                "approved_by": None,
                "executed_at": None,
                "expires_at": now,
                "request_metadata": "{}",
                "created_at": now,
            },
            {
                "id": 8,
                "identity_id": 43,
                "source_id": "analytics",
                "sql_text": "UPDATE t SET x = 1",
                "risk_level": "medium",
                "status": "pending",
                "approved_by": None,
                "executed_at": None,
                "expires_at": now,
                "request_metadata": "{}",
                "created_at": now,
            },
        ]
    )

    results = await queue.list_pending(limit=10)

    assert len(results) == 2
    assert results[0]["id"] == 7
    assert results[1]["id"] == 8
    mock_pool.fetch.assert_called_once()


# -- expire --


@pytest.mark.asyncio
async def test_expire_marks_old_entries(queue: ApprovalQueue, mock_pool: AsyncMock) -> None:
    """The sweep now RETURNINGs each row so it can be notified.

    A bare UPDATE could only report a count, which is enough to log but not to
    tell anyone which approvals lapsed.
    """
    mock_pool.fetch = AsyncMock(
        return_value=[
            {
                "id": index,
                "identity_id": 1,
                "source_id": "warehouse",
                "sql_text": "DELETE FROM t WHERE id = 1",
                "risk_level": "medium",
                "expires_at": None,
                "request_metadata": {},
            }
            for index in (1, 2, 3)
        ]
    )

    count = await queue._expire_old_approvals()

    assert count == 3
    mock_pool.fetch.assert_called_once()
    sql = mock_pool.fetch.call_args[0][0]
    assert "expired" in sql.lower()
    assert "returning" in sql.lower()


class _CapturingNotifier:
    """Records events instead of delivering them."""

    def __init__(self, explode: bool = False) -> None:
        self.events: list[Any] = []
        self._explode = explode

    def notify(self, event: Any) -> None:
        self.events.append(event)
        if self._explode:
            raise RuntimeError("notifier is broken")


@pytest.mark.asyncio
async def test_submit_emits_exactly_one_pending_event(mock_pool: AsyncMock) -> None:
    notifier = _CapturingNotifier()
    queue = ApprovalQueue(mock_pool, expiry_seconds=900, notifier=notifier)

    with pytest.raises(WriteApprovalRequiredError):
        await queue.submit(
            identity_id=42,
            source_id="warehouse",
            sql_text="UPDATE customers SET email = 'ada@example.com' WHERE id = 987654",
            risk_level=RiskLevel.MEDIUM,
            metadata={"identity_name": "analyst", "protocol": "mcp"},
        )

    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.kind == "pending"
    assert event.source_id == "warehouse"
    assert event.risk_level == "medium"
    # The event carries a fingerprint, never the statement.
    assert "ada@example.com" not in event.statement_fingerprint
    assert "987654" not in event.statement_fingerprint
    assert event.expires_at is not None


@pytest.mark.asyncio
async def test_a_broken_notifier_never_fails_the_submit(mock_pool: AsyncMock) -> None:
    """The write was already queued; a notification problem cannot undo that."""
    notifier = _CapturingNotifier(explode=True)
    queue = ApprovalQueue(mock_pool, expiry_seconds=900, notifier=notifier)

    with pytest.raises(WriteApprovalRequiredError):
        await queue.submit(
            identity_id=1,
            source_id="warehouse",
            sql_text="DELETE FROM t WHERE id = 1",
            risk_level=RiskLevel.HIGH,
        )


@pytest.mark.asyncio
async def test_submitting_without_a_notifier_is_unchanged(mock_pool: AsyncMock) -> None:
    queue = ApprovalQueue(mock_pool, expiry_seconds=900)

    with pytest.raises(WriteApprovalRequiredError):
        await queue.submit(
            identity_id=1,
            source_id="warehouse",
            sql_text="DELETE FROM t WHERE id = 1",
            risk_level=RiskLevel.HIGH,
        )


@pytest.mark.asyncio
async def test_expiry_emits_one_event_per_expired_row(mock_pool: AsyncMock) -> None:
    notifier = _CapturingNotifier()
    queue = ApprovalQueue(mock_pool, expiry_seconds=900, notifier=notifier)
    mock_pool.fetch = AsyncMock(
        return_value=[
            {
                "id": index,
                "identity_id": 1,
                "source_id": "warehouse",
                "sql_text": "DELETE FROM t WHERE id = 1",
                "risk_level": "medium",
                "expires_at": None,
                "request_metadata": {},
            }
            for index in (11, 12)
        ]
    )

    count = await queue._expire_old_approvals()

    assert count == 2
    assert [event.approval_id for event in notifier.events] == [11, 12]
    assert {event.kind for event in notifier.events} == {"expired"}


@pytest.mark.asyncio
async def test_reject_emits_the_outcome(mock_pool: AsyncMock) -> None:
    notifier = _CapturingNotifier()
    queue = ApprovalQueue(mock_pool, expiry_seconds=900, notifier=notifier)
    mock_pool.execute = AsyncMock(return_value="UPDATE 1")
    mock_pool.fetchrow = AsyncMock(
        return_value={
            "id": 9,
            "identity_id": 1,
            "source_id": "warehouse",
            "sql_text": "DELETE FROM t WHERE id = 1",
            "risk_level": "high",
            "request_metadata": {},
        }
    )

    await queue.reject(9, rejected_by="admin@acme.com")

    assert [event.kind for event in notifier.events] == ["rejected"]
    assert notifier.events[0].actor == "admin@acme.com"


@pytest.mark.asyncio
async def test_the_sweep_interval_comes_from_the_constructor(mock_pool: AsyncMock) -> None:
    queue = ApprovalQueue(mock_pool, expiry_seconds=3600, expiry_sweep_interval_seconds=5)

    assert queue._expiry_seconds == 3600
    assert queue._expiry_sweep_interval_seconds == 5
