"""Unit tests for the Approval Management API endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.config import InterLockConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> InterLockConfig:
    return InterLockConfig()


NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
EXPIRES = datetime.now(UTC) + timedelta(minutes=15)


def _make_approval(
    id: int = 1,
    status: str = "pending",
    approved_by: str | None = None,
) -> dict:
    return {
        "id": id,
        "identity_id": 10,
        "source_id": "warehouse",
        "sql_text": "UPDATE accounts SET balance = 0",
        "risk_level": "high",
        "status": status,
        "approved_by": approved_by,
        "executed_at": None,
        "expires_at": EXPIRES,
        "request_metadata": json.dumps({}),
        "created_at": NOW,
    }


class FakeRecord:
    """Minimal asyncpg.Record stand-in."""

    def __init__(self, data: dict):
        self._data = dict(data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __iter__(self):
        return iter(self._data.keys())

    def __getitem__(self, key):
        return self._data[key]


class FakeConnection:
    """Mock asyncpg connection with configurable return values."""

    def __init__(self):
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=None)
        self.execute = AsyncMock(return_value="UPDATE 1")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakePool:
    """Mock asyncpg pool."""

    def __init__(self):
        self.conn = FakeConnection()
        self.execute = AsyncMock(return_value="INSERT 0 1")
        self.close = AsyncMock()

    def acquire(self):
        return self.conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pool():
    return FakePool()


@pytest.fixture
def app(mock_pool):
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as mock_create_pg,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch(
            "interlock.admin.app.create_redis_client", new_callable=AsyncMock
        ) as mock_create_redis,
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        mock_create_pg.return_value = mock_pool
        mock_redis = MagicMock()
        mock_create_redis.return_value = mock_redis
        application = create_app(config=_make_config())
        application.state.pg_pool = mock_pool
        application.state.redis = mock_redis
        application.state.auth_disabled = True
        yield application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/approvals
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_approvals_empty(client, mock_pool):
    mock_pool.conn.fetch = AsyncMock(return_value=[])
    resp = await client.get("/api/approvals")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_approvals_returns_items(client, mock_pool):
    row = FakeRecord(_make_approval())
    mock_pool.conn.fetch = AsyncMock(return_value=[row])
    resp = await client.get("/api/approvals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == 1
    assert data[0]["status"] == "pending"


@pytest.mark.anyio
async def test_list_approvals_filters_by_status(client, mock_pool):
    row = FakeRecord(_make_approval(status="pending"))
    mock_pool.conn.fetch = AsyncMock(return_value=[row])
    resp = await client.get("/api/approvals?status=pending")
    assert resp.status_code == 200
    # Verify the filtered SQL was called (second arg is the status)
    call_args = mock_pool.conn.fetch.call_args
    assert call_args[0][1] == "pending"


@pytest.mark.anyio
async def test_list_approvals_invalid_status(client, mock_pool):
    resp = await client.get("/api/approvals?status=bogus")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/approvals/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_approval_found(client, mock_pool):
    row = FakeRecord(_make_approval(id=42))
    mock_pool.conn.fetchrow = AsyncMock(return_value=row)
    resp = await client.get("/api/approvals/42")
    assert resp.status_code == 200
    assert resp.json()["id"] == 42


@pytest.mark.anyio
async def test_get_approval_redacts_sql_literals_and_request_metadata(client, mock_pool):
    approval = _make_approval(id=43)
    approval["sql_text"] = "UPDATE users SET password = 'super-secret' WHERE id = 1234567"
    approval["request_metadata"] = json.dumps(
        {
            "headers": {"authorization": "Bearer secret", "accept": "application/json"},
            "body": {
                "encoding": "json",
                "data": {"password": "secret", "note": "safe"},
            },
        }
    )
    mock_pool.conn.fetchrow = AsyncMock(return_value=FakeRecord(approval))

    resp = await client.get("/api/approvals/43")

    assert resp.status_code == 200
    payload = resp.json()
    assert "super-secret" not in json.dumps(payload)
    assert "Bearer secret" not in json.dumps(payload)
    assert payload["sql_text"] == "UPDATE users SET password = '[REDACTED]' WHERE id = [REDACTED]"
    assert payload["request_metadata"]["headers"]["authorization"] == "[REDACTED]"
    assert payload["request_metadata"]["body"]["data"]["password"] == "[REDACTED]"


@pytest.mark.anyio
async def test_get_approval_not_found(client, mock_pool):
    mock_pool.conn.fetchrow = AsyncMock(return_value=None)
    resp = await client.get("/api/approvals/999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/approvals/{id}/approve
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_approve_success(client, mock_pool):
    pending_row = FakeRecord(_make_approval(id=5, status="pending"))
    mock_pool.conn.fetchrow = AsyncMock(return_value=pending_row)
    mock_pool.conn.execute = AsyncMock(return_value="UPDATE 1")

    resp = await client.post(
        "/api/approvals/5/approve",
        json={"approved_by": "admin@test.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 5
    assert data["status"] == "approved"


@pytest.mark.anyio
async def test_approve_not_found(client, mock_pool):
    mock_pool.conn.fetchrow = AsyncMock(return_value=None)

    resp = await client.post(
        "/api/approvals/999/approve",
        json={"approved_by": "admin@test.com"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_approve_non_pending_returns_409(client, mock_pool):
    approved_row = FakeRecord(_make_approval(id=5, status="approved", approved_by="someone"))
    mock_pool.conn.fetchrow = AsyncMock(return_value=approved_row)

    resp = await client.post(
        "/api/approvals/5/approve",
        json={"approved_by": "admin@test.com"},
    )
    assert resp.status_code == 409
    assert "not pending" in resp.json()["detail"]


@pytest.mark.anyio
async def test_approve_fires_notify(client, mock_pool):
    pending_row = FakeRecord(_make_approval(id=7, status="pending"))
    mock_pool.conn.fetchrow = AsyncMock(return_value=pending_row)
    mock_pool.conn.execute = AsyncMock(return_value="UPDATE 1")

    resp = await client.post(
        "/api/approvals/7/approve",
        json={"approved_by": "admin@test.com"},
    )
    assert resp.status_code == 200

    # Check that pg_notify was called (second execute call uses pg_notify)
    execute_calls = mock_pool.conn.execute.call_args_list
    notify_called = any("pg_notify" in str(call) for call in execute_calls)
    assert notify_called, f"Expected pg_notify call, got: {execute_calls}"


@pytest.mark.anyio
async def test_write_safety_htmx_approve_form_returns_redirect(client, mock_pool):
    pending_row = FakeRecord(_make_approval(id=8, status="pending"))
    mock_pool.conn.fetchrow = AsyncMock(return_value=pending_row)
    mock_pool.conn.execute = AsyncMock(return_value="UPDATE 1")

    resp = await client.post(
        "/api/approvals/8/approve",
        data={"approved_by": "security@test.com", "comment": "reviewed"},
        headers={"HX-Request": "true"},
    )

    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/dashboard/write-safety"
    approve_call = mock_pool.conn.execute.call_args_list[0]
    assert approve_call.args[2] == "security@test.com"
    audit_args = mock_pool.execute.await_args.args
    assert audit_args[3] == "approval.approve"
    assert audit_args[4] == "write_approval"
    detail = audit_args[6]
    assert detail["before"]["status"] == "pending"
    assert detail["before"]["sql_text"] == "UPDATE accounts SET balance = 0"
    assert detail["after"]["status"] == "approved"
    assert detail["actor"] == "security@test.com"
    assert detail["comment"] == "reviewed"


# ---------------------------------------------------------------------------
# POST /api/approvals/{id}/reject
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reject_success(client, mock_pool):
    pending_row = FakeRecord(_make_approval(id=3, status="pending"))
    mock_pool.conn.fetchrow = AsyncMock(return_value=pending_row)
    mock_pool.conn.execute = AsyncMock(return_value="UPDATE 1")

    resp = await client.post(
        "/api/approvals/3/reject",
        json={"rejected_by": "security@test.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 3
    assert data["status"] == "rejected"


@pytest.mark.anyio
async def test_write_safety_htmx_reject_form_returns_redirect(client, mock_pool):
    pending_row = FakeRecord(
        {
            **_make_approval(id=9, status="pending"),
            "sql_text": "DELETE FROM customers WHERE ssn = '123-45-6789'",
            "request_metadata": json.dumps({"headers": {"authorization": "Bearer secret-token"}}),
        }
    )
    mock_pool.conn.fetchrow = AsyncMock(return_value=pending_row)
    mock_pool.conn.execute = AsyncMock(return_value="UPDATE 1")

    resp = await client.post(
        "/api/approvals/9/reject",
        data={"rejected_by": "security@test.com", "comment": "not enough context"},
        headers={"HX-Request": "true"},
    )

    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/dashboard/write-safety"
    reject_call = mock_pool.conn.execute.call_args_list[0]
    assert reject_call.args[2] == "security@test.com"
    audit_args = mock_pool.execute.await_args.args
    assert audit_args[3] == "approval.reject"
    assert audit_args[4] == "write_approval"
    detail = audit_args[6]
    assert detail["before"]["sql_text"] == "DELETE FROM customers WHERE ssn = '[REDACTED]'"
    assert detail["before"]["request_metadata"]["headers"]["authorization"] == "[REDACTED]"
    assert detail["after"]["status"] == "rejected"
    assert detail["comment"] == "not enough context"
    assert "secret-token" not in str(detail)


@pytest.mark.anyio
async def test_reject_non_pending_returns_409(client, mock_pool):
    executed_row = FakeRecord(_make_approval(id=3, status="executed"))
    mock_pool.conn.fetchrow = AsyncMock(return_value=executed_row)

    resp = await client.post(
        "/api/approvals/3/reject",
        json={"rejected_by": "security@test.com"},
    )
    assert resp.status_code == 409
