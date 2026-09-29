"""Alerting tests: evaluator + dashboard routes + history acknowledge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.alerts import evaluate_rule
from interlock.admin.app import create_app
from interlock.config import InterLockConfig

# ---------------------------------------------------------------------------
# Evaluator unit tests
# ---------------------------------------------------------------------------


class _Pool:
    """Minimal pool that returns a configured fetchval value."""

    def __init__(self, value=0.0):
        self.value = value
        self.last_sql: str | None = None
        self.last_params: tuple = ()

    async def fetchval(self, sql, *args):
        self.last_sql = sql
        self.last_params = args
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


@pytest.mark.asyncio
async def test_evaluator_request_volume_fires_when_over_threshold() -> None:
    pool = _Pool(value=120.0)
    result = await evaluate_rule(
        pool,
        {
            "condition_type": "request_volume",
            "comparator": ">",
            "threshold": 100.0,
            "window_seconds": 300,
        },
    )
    assert result.fired is True
    assert result.observed_value == 120.0
    assert "FIRED" in result.message


@pytest.mark.asyncio
async def test_evaluator_does_not_fire_below_threshold() -> None:
    pool = _Pool(value=12.0)
    result = await evaluate_rule(
        pool,
        {
            "condition_type": "request_volume",
            "comparator": ">",
            "threshold": 100.0,
            "window_seconds": 300,
        },
    )
    assert result.fired is False
    assert "FIRED" not in result.message


@pytest.mark.asyncio
async def test_evaluator_p95_latency_uses_percentile_query() -> None:
    pool = _Pool(value=180.0)
    result = await evaluate_rule(
        pool,
        {
            "condition_type": "p95_latency_ms",
            "comparator": ">",
            "threshold": 100.0,
            "window_seconds": 300,
        },
    )
    assert result.fired is True
    assert "PERCENTILE_CONT(0.95)" in (pool.last_sql or "")


@pytest.mark.asyncio
async def test_evaluator_queue_depth_filters_to_pending() -> None:
    pool = _Pool(value=5.0)
    result = await evaluate_rule(
        pool,
        {
            "condition_type": "queue_depth",
            "comparator": ">=",
            "threshold": 3.0,
        },
    )
    assert result.fired is True
    assert "write_approval_queue" in (pool.last_sql or "")
    assert "status = 'pending'" in (pool.last_sql or "")


@pytest.mark.asyncio
async def test_evaluator_error_rate_returns_zero_on_empty_window() -> None:
    pool = _Pool(value=0.0)
    result = await evaluate_rule(
        pool,
        {
            "condition_type": "error_rate",
            "comparator": ">",
            "threshold": 0.05,
            "window_seconds": 60,
        },
    )
    assert result.fired is False
    assert result.observed_value == 0.0


@pytest.mark.asyncio
async def test_evaluator_handles_unknown_condition() -> None:
    pool = _Pool()
    result = await evaluate_rule(
        pool,
        {
            "condition_type": "nonsense",
            "comparator": ">",
            "threshold": 0.0,
        },
    )
    assert result.fired is False
    assert "unknown" in result.message


@pytest.mark.asyncio
async def test_evaluator_handles_unknown_comparator() -> None:
    pool = _Pool(value=1.0)
    result = await evaluate_rule(
        pool,
        {
            "condition_type": "request_volume",
            "comparator": "??",
            "threshold": 0.0,
            "window_seconds": 60,
        },
    )
    assert result.fired is False
    assert "comparator" in result.message


@pytest.mark.asyncio
async def test_evaluator_handles_query_failure_gracefully() -> None:
    pool = _Pool(value=RuntimeError("boom"))
    result = await evaluate_rule(
        pool,
        {
            "condition_type": "request_volume",
            "comparator": ">",
            "threshold": 0.0,
            "window_seconds": 60,
        },
    )
    assert result.fired is False
    assert "query failed" in result.message


@pytest.mark.asyncio
async def test_evaluator_scope_source_id_binds_filter() -> None:
    pool = _Pool(value=10.0)
    await evaluate_rule(
        pool,
        {
            "condition_type": "request_volume",
            "comparator": ">",
            "threshold": 5.0,
            "window_seconds": 60,
            "source_id": "shop",
        },
    )
    assert "source_id = $2" in (pool.last_sql or "")
    # window + source_id
    assert pool.last_params == (60, "shop")


# ---------------------------------------------------------------------------
# Dashboard route integration
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
        self.fetchrow_result = None
        self.fetchval_result = None

    async def fetch(self, *a, **k):
        return self.fetch_result

    async def fetchrow(self, *a, **k):
        return self.fetchrow_result

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


@pytest.mark.anyio
async def test_alerts_page_renders_empty_when_no_rules(client, pool):
    pool.fetch_result = []
    resp = await client.get("/dashboard/alerts")
    assert resp.status_code == 200
    assert "No alert rules configured" in resp.text
    assert "Delivery is log-only unless a channel below is explicitly configured" in resp.text


@pytest.mark.anyio
async def test_alerts_page_masks_notification_targets(client, pool):
    pool.fetch_result = [
        {
            "id": 7,
            "name": "webhook",
            "condition_type": "error_rate",
            "comparator": ">",
            "threshold": 0.1,
            "window_seconds": 60,
            "source_id": None,
            "identity_id": None,
            "notification_channel": "webhook",
            "notification_target": "https://hooks.example.test/path?token=top-secret",
            "enabled": True,
            "last_evaluated_at": None,
            "last_fired_at": None,
            "rule_name": "webhook",
            "fired_at": None,
            "observed_value": None,
            "message": "safe",
            "acknowledged_at": None,
            "acknowledged_by": None,
        }
    ]

    resp = await client.get("/dashboard/alerts")

    assert resp.status_code == 200
    assert "top-secret" not in resp.text
    assert "/path" not in resp.text
    assert "https://hooks.example.test/[REDACTED]" in resp.text


@pytest.mark.anyio
async def test_new_alert_form_renders(client):
    resp = await client.get("/dashboard/alerts/new")
    assert resp.status_code == 200
    assert "New Alert Rule" in resp.text
    assert 'name="condition_type"' in resp.text


@pytest.mark.anyio
async def test_create_alert_rule_inserts_and_redirects(client, pool):
    pool.fetchval_result = None
    resp = await client.post(
        "/dashboard/alerts/create",
        data={
            "name": "high-error",
            "description": "desc",
            "condition_type": "error_rate",
            "comparator": ">",
            "threshold": "0.05",
            "window_seconds": "300",
            "source_id": "shop",
            "identity_id": "",
            "notification_channel": "slack",
            "notification_target": "https://hooks.slack/x",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/alerts"
    inserts = [c for c in pool._calls if "INSERT INTO alert_rules" in c[1][0]]
    assert inserts
    args = inserts[0][1]
    # name, description, condition_type, comparator, threshold, window, source, identity, channel, target
    assert args[1] == "high-error"
    assert args[3] == "error_rate"
    assert args[5] == 0.05
    assert args[7] == "shop"
    assert args[8] is None  # identity_id blank


@pytest.mark.anyio
async def test_create_alert_rejects_duplicate_name(client, pool):
    pool.fetchval_result = 1
    resp = await client.post(
        "/dashboard/alerts/create",
        data={
            "name": "dup",
            "description": "",
            "condition_type": "error_rate",
            "comparator": ">",
            "threshold": "0.1",
            "window_seconds": "60",
            "source_id": "",
            "identity_id": "",
            "notification_channel": "log",
            "notification_target": "https://hooks.example.test/path?token=top-secret",
        },
    )
    assert resp.status_code == 200
    assert "already exists" in resp.text
    assert "top-secret" not in resp.text
    assert "A target was supplied but is not redisplayed" in resp.text


@pytest.mark.anyio
async def test_evaluate_now_fires_and_records_history(client, pool):
    # Rule row returned by fetchrow; fetchval returns the observed
    # value via evaluate_rule's internal query.
    class FetchrowOnlyPool(FakePool):
        async def fetchval(self, sql, *args):
            return 200.0  # observed value

    pool = FetchrowOnlyPool()
    pool.fetchrow_result = {
        "id": 1,
        "name": "high-volume",
        "condition_type": "request_volume",
        "comparator": ">",
        "threshold": 100.0,
        "window_seconds": 60,
        "source_id": None,
        "identity_id": None,
    }
    app = _make_app(pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/dashboard/alerts/1/evaluate")
    assert resp.status_code == 200
    body = resp.text
    assert "FIRED" in body
    # last_evaluated_at update + alert_history insert + last_fired_at update
    updates = [c for c in pool._calls if "UPDATE alert_rules SET last_evaluated_at" in c[1][0]]
    fires = [c for c in pool._calls if "INSERT INTO alert_history" in c[1][0]]
    fired_updates = [c for c in pool._calls if "UPDATE alert_rules SET last_fired_at" in c[1][0]]
    assert updates and fires and fired_updates


@pytest.mark.anyio
async def test_evaluate_now_unknown_rule_returns_error(client, pool):
    pool.fetchrow_result = None
    resp = await client.post("/dashboard/alerts/999/evaluate")
    assert resp.status_code == 200
    assert "not found" in resp.text


@pytest.mark.anyio
async def test_toggle_rule_flips_enabled(client, pool):
    resp = await client.post("/dashboard/alerts/5/toggle", follow_redirects=False)
    assert resp.status_code == 303
    toggles = [c for c in pool._calls if "SET enabled = NOT enabled" in c[1][0]]
    assert toggles


@pytest.mark.anyio
async def test_acknowledge_marks_history_row(client, pool):
    resp = await client.post("/dashboard/alerts/history/42/ack", follow_redirects=False)
    assert resp.status_code == 303
    acks = [
        c
        for c in pool._calls
        if "UPDATE alert_history" in c[1][0] and "acknowledged_at = NOW()" in c[1][0]
    ]
    assert acks
