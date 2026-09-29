"""Tests for the policy visual builder.

Covers the form-field -> JSONB translator and the dashboard routes
that surface the policy form on the access-control page.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.admin.routes.dashboard import _build_policy_jsonb
from interlock.config import InterLockConfig

# ---------------------------------------------------------------------------
# _build_policy_jsonb unit tests
# ---------------------------------------------------------------------------


def _builder(**kw):
    defaults = {
        "cond_source_id": "",
        "cond_operation": "",
        "cond_roles": "",
        "cond_identity_roles": "",
        "cond_tables": "",
        "action_effect": "allow",
        "action_rate_limit": "",
        "action_redact_columns": "",
        "raw_conditions": "",
        "raw_actions": "",
    }
    defaults.update(kw)
    return _build_policy_jsonb(**defaults)


def test_builder_emits_minimal_allow_when_nothing_set() -> None:
    conds, acts, err = _builder()
    assert err is None
    assert conds == {}
    assert acts == {"effect": "allow"}


def test_builder_translates_field_inputs_into_lists() -> None:
    conds, acts, err = _builder(
        cond_source_id="shop",
        cond_operation="write",
        cond_roles="analyst, admin",
        cond_identity_roles="platform-admin",
        cond_tables="orders,customers",
        action_effect="deny",
        action_redact_columns="ssn, email",
        action_rate_limit="60",
    )
    assert err is None
    assert conds["source_id"] == "shop"
    assert conds["operation_types"] == ["write"]
    assert conds["roles"] == ["analyst", "admin"]
    assert conds["identity_roles"] == ["platform-admin"]
    assert conds["tables"] == ["orders", "customers"]
    assert acts["effect"] == "deny"
    assert acts["redact_columns"] == ["ssn", "email"]
    assert acts["rate_limit"] == 60


def test_builder_raw_conditions_override_fields() -> None:
    conds, acts, err = _builder(
        cond_roles="ignored",
        raw_conditions='{"roles": ["override"], "custom": 1}',
    )
    assert err is None
    assert conds == {"roles": ["override"], "custom": 1}


def test_builder_raw_actions_override_fields() -> None:
    conds, acts, err = _builder(
        action_effect="allow",
        raw_actions='{"effect": "deny", "log_only": true}',
    )
    assert err is None
    assert acts == {"effect": "deny", "log_only": True}


def test_builder_rejects_invalid_raw_conditions_json() -> None:
    _, _, err = _builder(raw_conditions="{not-json")
    assert err and "conditions invalid JSON" in err


def test_builder_rejects_non_object_raw_actions() -> None:
    _, _, err = _builder(raw_actions="[1,2,3]")
    assert err and "actions must be a JSON object" in err


def test_builder_rejects_non_integer_rate_limit() -> None:
    _, _, err = _builder(action_rate_limit="lots")
    assert err and "rate_limit" in err


# ---------------------------------------------------------------------------
# Dashboard route integration
# ---------------------------------------------------------------------------


class FakeConn:
    def __init__(self, parent):
        self._parent = parent

    async def execute(self, *args, **kw):
        self._parent._calls.append(("execute", args))
        return "OK"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
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

    async def execute(self, *args, **kw):
        self._calls.append(("execute", args))
        return "OK"

    def acquire(self):
        return FakeConn(self)


def _make_app(pool):
    config = InterLockConfig()
    redis = MagicMock()
    redis.aclose = AsyncMock()
    redis.delete = AsyncMock()
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
async def test_new_policy_form_renders(client):
    resp = await client.get("/dashboard/policies/new")
    assert resp.status_code == 200
    body = resp.text
    assert "New Policy" in body
    assert 'name="cond_roles"' in body
    assert 'name="cond_identity_roles"' in body
    assert 'name="action_effect"' in body


@pytest.mark.anyio
async def test_create_policy_persists_and_notifies(client, pool):
    pool.fetchval_result = None  # no duplicate
    resp = await client.post(
        "/dashboard/policies/create",
        data={
            "name": "block-pii-writes",
            "priority": "50",
            "cond_source_id": "shop",
            "cond_operation": "write",
            "cond_roles": "analyst",
            "cond_tables": "customers",
            "action_effect": "deny",
            "action_rate_limit": "",
            "action_redact_columns": "",
            "raw_conditions": "",
            "raw_actions": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    inserts = [c for c in pool._calls if "INSERT INTO policy_rules" in c[1][0]]
    assert inserts, "policy was not inserted"
    notifies = [c for c in pool._calls if "NOTIFY onyx_policy_changed" in c[1][0]]
    assert notifies, "policy NOTIFY was not fired"


@pytest.mark.anyio
async def test_create_policy_rejects_duplicate_name(client, pool):
    pool.fetchval_result = 1  # duplicate present
    resp = await client.post(
        "/dashboard/policies/create",
        data={
            "name": "dup",
            "priority": "10",
            "cond_source_id": "",
            "cond_operation": "",
            "cond_roles": "",
            "cond_tables": "",
            "action_effect": "allow",
            "action_rate_limit": "",
            "action_redact_columns": "",
            "raw_conditions": "",
            "raw_actions": "",
        },
    )
    assert resp.status_code == 200
    assert "already exists" in resp.text
    inserts = [c for c in pool._calls if "INSERT INTO policy_rules" in c[1][0]]
    assert not inserts


@pytest.mark.anyio
async def test_create_policy_shows_error_on_bad_raw_json(client, pool):
    pool.fetchval_result = None
    resp = await client.post(
        "/dashboard/policies/create",
        data={
            "name": "bad-json",
            "priority": "0",
            "cond_source_id": "",
            "cond_operation": "",
            "cond_roles": "",
            "cond_tables": "",
            "action_effect": "allow",
            "action_rate_limit": "",
            "action_redact_columns": "",
            "raw_conditions": "{not json",
            "raw_actions": "",
        },
    )
    assert resp.status_code == 200
    assert "invalid JSON" in resp.text


@pytest.mark.anyio
async def test_edit_policy_form_prefills_from_row(client, pool):
    pool.fetchrow_result = MagicMock()
    pool.fetchrow_result.__getitem__ = lambda self, k: {
        "id": 9,
        "name": "audit-pii",
        "priority": 42,
        "conditions": {"roles": ["analyst"], "tables": ["customers"]},
        "actions": {"effect": "allow", "redact_columns": ["ssn"]},
    }[k]
    resp = await client.get("/dashboard/policies/9/edit")
    assert resp.status_code == 200
    body = resp.text
    assert 'value="audit-pii"' in body
    assert 'value="analyst"' in body
    assert "ssn" in body
