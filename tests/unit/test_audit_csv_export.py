"""Tests for the audit-explorer CSV export and broadened filters."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.admin.routes.dashboard import _build_audit_filter
from interlock.config import InterLockConfig

# ---------------------------------------------------------------------------
# _build_audit_filter unit
# ---------------------------------------------------------------------------


def test_filter_default_returns_empty_where() -> None:
    where, params = _build_audit_filter()
    assert where == ""
    assert params == []


def test_filter_basic_three_fields() -> None:
    where, params = _build_audit_filter(
        source="shop",
        operation="read",
        status="success",
    )
    assert "source_id = $1" in where
    assert "operation = $2" in where
    assert "status = $3" in where
    assert params == ["shop", "read", "success"]


def test_filter_identity_coerces_to_int() -> None:
    where, params = _build_audit_filter(identity_id="42")
    assert "identity_id = $1" in where
    assert params == [42]


def test_filter_identity_silently_drops_non_numeric() -> None:
    where, params = _build_audit_filter(identity_id="not-a-number")
    assert where == ""
    assert params == []


def test_filter_cache_tier_miss_is_null_check() -> None:
    where, params = _build_audit_filter(cache_tier="miss")
    assert "cache_tier IS NULL" in where
    assert params == []


def test_filter_cache_tier_value_binds() -> None:
    where, params = _build_audit_filter(cache_tier="l1")
    assert "cache_tier = $1" in where
    assert params == ["l1"]


def test_filter_pii_yes_and_no_translate_to_boolean() -> None:
    yes, _ = _build_audit_filter(pii="yes")
    no, _ = _build_audit_filter(pii="no")
    assert "pii_detected = TRUE" in yes
    assert "pii_detected = FALSE" in no


def test_filter_since_and_until() -> None:
    where, params = _build_audit_filter(
        since="2026-05-15T00:00:00Z",
        until="2026-05-15T23:59:59Z",
    )
    assert "created_at >= $1::timestamptz" in where
    assert "created_at <= $2::timestamptz" in where
    assert params == ["2026-05-15T00:00:00Z", "2026-05-15T23:59:59Z"]


# ---------------------------------------------------------------------------
# CSV export route
# ---------------------------------------------------------------------------


class FakeConn:
    def __init__(self, parent):
        self._parent = parent

    async def execute(self, *a, **k):
        self._parent._calls.append(("execute", a))
        return "OK"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


class FakePool:
    def __init__(self):
        self._calls = []
        self.fetch_result = []
        self.fetchval_result = 0

    async def fetch(self, *a, **k):
        return self.fetch_result

    async def fetchrow(self, *a, **k):
        return None

    async def fetchval(self, *a, **k):
        return self.fetchval_result

    async def execute(self, *a, **k):
        self._calls.append(("execute", a))
        return "OK"

    def acquire(self):
        return FakeConn(self)


def _make_app(pool):
    config = InterLockConfig()
    redis = MagicMock()
    redis.aclose = AsyncMock()
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as cp,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch("interlock.admin.app.create_redis_client", new_callable=AsyncMock) as cr,
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        cp.return_value = pool
        cr.return_value = redis
        app = create_app(config=config)
    app.state.pg_pool = pool
    app.state.redis = redis
    app.state.auth_disabled = True
    return app


@pytest.fixture
def pool():
    return FakePool()


@pytest.fixture
def app(pool):
    return _make_app(pool)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class _AuditRow(dict):
    pass


def _audit(**kw):
    base = dict(
        id=1,
        created_at=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        identity_id=2,
        source_id="shop",
        operation="read",
        sql_fingerprint="abc123",
        cache_hit=True,
        cache_tier="l1",
        latency_ms=3.4,
        pii_detected=False,
        pii_types=[],
        risk_level=None,
        status="success",
        error_message=None,
    )
    base.update(kw)
    return _AuditRow(base)


@pytest.mark.anyio
async def test_csv_export_includes_header_and_rows(client, pool):
    pool.fetch_result = [
        _audit(id=1, source_id="shop"),
        _audit(
            id=2,
            source_id="analytics",
            pii_detected=True,
            pii_types=["SSN", "EMAIL"],
            status="denied",
            identity_name="analyst-claude",
        ),
    ]
    resp = await client.get("/dashboard/audit-costs/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.text
    lines = body.strip().split("\r\n") or body.strip().split("\n")
    # Header + 2 rows.
    assert lines[0].startswith("id,created_at,identity_id,source_id,operation")
    # New columns are appended so positional consumers of the old ones still work.
    assert lines[0].endswith(",error_message,identity_name,protocol,correlation_id")
    assert any(",analyst-claude," in line for line in lines[1:])
    assert any("shop" in line for line in lines[1:])
    assert any("analytics" in line for line in lines[1:])
    # PII types come through as comma-joined inside the cell.
    assert any("SSN,EMAIL" in line for line in lines)


@pytest.mark.anyio
async def test_csv_export_respects_source_filter(client, pool):
    pool.fetch_result = [_audit(source_id="shop")]
    resp = await client.get("/dashboard/audit-costs/export.csv?source=shop")
    assert resp.status_code == 200
    # The fake pool returns whatever we set, so we can only assert the
    # response was successful and the filename header is present.
    assert "filename=" in resp.headers["content-disposition"]


@pytest.mark.anyio
async def test_audit_log_partial_accepts_new_filters(client, pool):
    pool.fetchval_result = 0
    pool.fetch_result = []
    resp = await client.get(
        "/dashboard/audit-costs/log"
        "?cache_tier=l1&pii=yes&identity_id=7"
        "&since=2026-01-01T00:00:00Z&until=2026-06-01T00:00:00Z"
    )
    assert resp.status_code == 200


def test_filter_protocol_binds() -> None:
    """The audit concept page promises a protocol filter; this is it."""
    where, params = _build_audit_filter(protocol="postgresql")
    assert "protocol = $1" in where
    assert params == ["postgresql"]


@pytest.mark.anyio
async def test_csv_export_carries_protocol_and_correlation_id(client, pool):
    pool.fetch_result = [_audit(id=3, protocol="mcp", correlation_id="corr-abc")]
    resp = await client.get("/dashboard/audit-costs/export.csv?protocol=mcp")
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    assert lines[0].endswith(",protocol,correlation_id")
    assert lines[1].endswith(",mcp,corr-abc")
