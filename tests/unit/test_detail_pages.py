"""Source-detail, identity-detail, and key-rotation dashboard tests."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.config import InterLockConfig

# ---------------------------------------------------------------------------
# Test fakes
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


class _Dispatch(dict):
    """Marker class so the FakePool can tell a test's query->payload
    dispatch table apart from a single row (also a dict subclass)."""


class FakePool:
    def __init__(self):
        self._calls = []
        self.fetch_result = []
        self.fetchrow_result = None
        self.fetchval_result = None

    async def fetch(self, *a, **k):
        if isinstance(self.fetch_result, _Dispatch):
            for needle, payload in self.fetch_result.items():
                if needle in a[0]:
                    return payload
            return []
        return self.fetch_result

    async def fetchrow(self, *a, **k):
        if isinstance(self.fetchrow_result, _Dispatch):
            for needle, payload in self.fetchrow_result.items():
                if needle in a[0]:
                    return payload
            return None
        return self.fetchrow_result

    async def fetchval(self, *a, **k):
        return self.fetchval_result

    async def execute(self, *a, **k):
        self._calls.append(("execute", a))
        return "OK"

    def acquire(self):
        return FakeConn(self)


class _Row(dict):
    def __getitem__(self, k):
        return super().__getitem__(k)


def _row(**kw):
    return _Row(kw)


def _dispatch(**kw):
    return _Dispatch(kw)


def _make_app(pool, redis=None):
    config = InterLockConfig()
    redis = redis or MagicMock()
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
def redis():
    r = MagicMock()
    r.delete = AsyncMock(return_value=1)
    r.aclose = AsyncMock()
    return r


@pytest.fixture
def app(pool, redis):
    return _make_app(pool, redis)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Source detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_source_detail_renders_for_existing_source(client, pool):
    pool.fetchrow_result = _dispatch(
        **{
            "FROM data_sources": _row(
                id=1,
                source_id="shop",
                name="Shop DB",
                source_type="postgresql",
                connection_config={"host": "postgres", "port": 5432},
                cache_strategy="deterministic_first",
                enabled=True,
                metadata={},
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            "FROM audit_log": _row(
                requests_24h=12,
                cache_hits=4,
                errors=1,
                denials=0,
                avg_ms=8.3,
                p95_ms=15.7,
            ),
        }
    )
    pool.fetch_result = _dispatch(
        **{
            "FROM audit_log": [
                _row(
                    created_at=datetime.now(UTC),
                    identity_id=2,
                    operation="read",
                    cache_hit=True,
                    cache_tier="l1",
                    latency_ms=4.1,
                    pii_detected=False,
                    status="success",
                    error_message=None,
                ),
            ],
        }
    )
    resp = await client.get("/dashboard/data-sources/shop")
    assert resp.status_code == 200
    body = resp.text
    assert "Shop DB" in body
    assert "postgres:5432" in body
    assert "12" in body and "33%" in body  # 4/12
    assert "8.3" in body and "15.7" in body
    # Live Test button present.
    assert 'hx-post="/dashboard/data-sources/shop/test"' in body
    assert 'href="/dashboard/data-sources/shop/edit"' in body


@pytest.mark.anyio
async def test_source_detail_returns_not_found_when_unknown(client, pool):
    pool.fetchrow_result = None
    resp = await client.get("/dashboard/data-sources/missing")
    assert resp.status_code == 200
    assert "Not found" in resp.text
    assert "missing" in resp.text


@pytest.mark.anyio
async def test_live_test_runs_probe_and_returns_strip(client, pool):
    pool.fetchrow_result = _row(
        source_type="postgresql",
        connection_config={"host": "127.0.0.1", "port": 1},
    )
    resp = await client.post("/dashboard/data-sources/shop/test")
    assert resp.status_code == 200
    body = resp.text
    # Either we got an OK strip or a Failure strip; both are valid responses
    # from a real probe. The bound port 1 will fail.
    assert ("Connection failed" in body) or ("Connected in" in body)
    audit_rows = [c for c in pool._calls if "INSERT INTO admin_audit_log" in c[1][0]]
    assert audit_rows
    assert audit_rows[-1][1][3] == "data_source.probe"


@pytest.mark.anyio
async def test_source_edit_form_masks_literal_secrets(client, pool):
    pool.fetchrow_result = _row(
        source_id="shop",
        name="Shop DB",
        source_type="postgresql",
        connection_config={
            "host": "postgres",
            "port": 5432,
            "database": "shop",
            "password": "super-secret-password",
            "password_ref": "env://SHOP_PASSWORD",
        },
        cache_strategy="deterministic_first",
        enabled=True,
        metadata={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    resp = await client.get("/dashboard/data-sources/shop/edit")
    assert resp.status_code == 200
    body = resp.text
    assert "Edit Data Source" in body
    assert "super-secret-password" not in body
    assert "env://SHOP_PASSWORD" in body
    assert 'name="config_key" value="password"' not in body


@pytest.mark.anyio
async def test_source_edit_post_updates_safe_fields_and_audits(client, pool):
    pool.fetchrow_result = _row(
        source_id="shop",
        name="Shop DB",
        source_type="postgresql",
        connection_config={"host": "postgres", "port": 5432, "password": "secret"},
        cache_strategy="deterministic_first",
        enabled=True,
        metadata={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    resp = await client.post(
        "/dashboard/data-sources/shop/edit",
        data={
            "name": "Shop DB Updated",
            "cache_strategy": "bypass",
            "enabled": "on",
            "config_key": ["host", "password"],
            "config_value": ["postgres-updated", "should-not-update"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/data-sources/shop"
    audit_rows = [c for c in pool._calls if "INSERT INTO admin_audit_log" in c[1][0]]
    assert audit_rows
    assert audit_rows[-1][1][3] == "data_source.edit"
    detail = audit_rows[-1][1][6]
    assert "password" not in detail["after"]["editable_config_keys"]


# ---------------------------------------------------------------------------
# Identity detail
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_identity_detail_renders(client, pool):
    pool.fetchrow_result = _dispatch(
        **{
            "FROM identities": _row(
                id=7,
                name="demo-agent",
                agent_type="custom",
                team="eng",
                roles=["analyst", "reader"],
                mapped_pg_role="onyx",
                enabled=True,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                last_used_at=None,
                rotated_at=None,
            ),
            "FROM audit_log": _row(
                requests_24h=8,
                denials=1,
                errors=0,
                cache_hits=3,
                p95_ms=22.5,
            ),
        }
    )
    pool.fetch_result = _dispatch(
        **{
            "ORDER BY created_at DESC": [
                _row(
                    created_at=datetime.now(UTC),
                    source_id="shop",
                    operation="read",
                    cache_hit=True,
                    cache_tier="l1",
                    latency_ms=3.0,
                    status="success",
                    error_message=None,
                ),
            ],
            "GROUP BY source_id": [_row(source_id="shop", cnt=8)],
        }
    )
    resp = await client.get("/dashboard/access-control/identities/7")
    assert resp.status_code == 200
    body = resp.text
    assert "demo-agent" in body
    assert "analyst" in body and "reader" in body
    # Rotate button is present.
    assert 'hx-post="/dashboard/access-control/identities/7/rotate-key"' in body
    # Top sources section.
    assert "shop" in body


@pytest.mark.anyio
async def test_identity_detail_not_found(client, pool):
    pool.fetchrow_result = None
    resp = await client.get("/dashboard/access-control/identities/999")
    assert resp.status_code == 200
    assert "Not found" in resp.text


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rotate_key_updates_hash_and_invalidates_session(client, pool, redis):
    old_hash = hashlib.sha256(b"old-key").hexdigest()
    pool.fetchrow_result = _row(
        id=7,
        name="demo-agent",
        roles=["analyst"],
        api_key_hash=old_hash,
    )
    resp = await client.post("/dashboard/access-control/identities/7/rotate-key")
    assert resp.status_code == 200
    body = resp.text
    # The reveal partial shows the new key once.
    assert "Identity created" in body
    assert "demo-agent" in body
    # UPDATE was issued against identities.
    updates = [
        c for c in pool._calls if "UPDATE identities" in c[1][0] and "api_key_hash" in c[1][0]
    ]
    assert updates, "key hash was not updated"
    # Old session cache key was deleted.
    redis.delete.assert_awaited()
    deleted_key = redis.delete.await_args.args[0]
    assert deleted_key == f"session:{old_hash}"


@pytest.mark.anyio
async def test_rotate_key_unknown_identity_returns_error(client, pool, redis):
    pool.fetchrow_result = None
    resp = await client.post("/dashboard/access-control/identities/404/rotate-key")
    assert resp.status_code == 200
    assert "not found" in resp.text
    redis.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Source edit: upstream TLS
# ---------------------------------------------------------------------------

_TLS_REQUIRED = "Verified upstream PostgreSQL TLS is required"


@pytest.fixture
async def production_client(app):
    app.state.config = app.state.config.model_copy(update={"environment": "production"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _pg_source_row(connection_config):
    return _row(
        source_id="shop",
        name="Shop DB",
        source_type="postgresql",
        connection_config=connection_config,
        cache_strategy="deterministic_first",
        enabled=True,
        metadata={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _record_updates(pool):
    """Capture the connection_config each UPDATE would store."""
    updates = []
    original = pool.fetchrow

    async def fetchrow(*a, **k):
        if "UPDATE data_sources" in a[0]:
            updates.append(a[3])
        return await original(*a, **k)

    pool.fetchrow = fetchrow
    return updates


def _tls_edit(sslmode, ssl_ca):
    return {
        "name": "Shop DB",
        "cache_strategy": "deterministic_first",
        "enabled": "on",
        "config_key": ["host", "sslmode", "ssl_ca"],
        "config_value": ["db.example.com", sslmode, ssl_ca],
    }


@pytest.mark.anyio
async def test_source_edit_form_offers_tls_fields_when_none_are_stored(client, pool):
    pool.fetchrow_result = _pg_source_row({"host": "postgres", "port": 5432})
    body = (await client.get("/dashboard/data-sources/shop/edit")).text
    assert 'name="config_key" value="sslmode"' in body
    assert 'name="config_key" value="ssl_ca"' in body
    assert "Production accepts only verify-full or verify-ca" in body


@pytest.mark.anyio
async def test_source_edit_form_shows_the_stored_tls_once(client, pool):
    pool.fetchrow_result = _pg_source_row(
        {"host": "postgres", "sslmode": "verify-ca", "ssl_ca": "/run/ca.crt"}
    )
    body = (await client.get("/dashboard/data-sources/shop/edit")).text
    assert body.count('name="config_key" value="sslmode"') == 1
    assert body.count('name="config_key" value="ssl_ca"') == 1
    assert re.search(r'value="verify-ca"\s+selected', body)
    assert 'value="/run/ca.crt"' in body


@pytest.mark.anyio
async def test_production_source_edit_refuses_unverified_tls(production_client, pool):
    pool.fetchrow_result = _pg_source_row({"host": "postgres", "sslmode": "verify-full"})
    updates = _record_updates(pool)
    resp = await production_client.post(
        "/dashboard/data-sources/shop/edit", data=_tls_edit("require", ""), follow_redirects=False
    )
    assert resp.status_code == 200
    assert 'class="form-error"' in resp.text
    assert _TLS_REQUIRED in resp.text
    assert updates == []
    assert not [c for c in pool._calls if "NOTIFY" in c[1][0]]


@pytest.mark.anyio
async def test_production_source_edit_stores_verified_tls(production_client, pool):
    pool.fetchrow_result = _pg_source_row({"host": "postgres"})
    updates = _record_updates(pool)
    resp = await production_client.post(
        "/dashboard/data-sources/shop/edit",
        data=_tls_edit("verify-full", "/run/secrets/shop-ca/ca.crt"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert updates[-1]["sslmode"] == "verify-full"
    assert updates[-1]["ssl_ca"] == "/run/secrets/shop-ca/ca.crt"


@pytest.mark.anyio
async def test_source_edit_tls_replaces_a_spelling_that_would_shadow_it(client, pool):
    # `ssl` outranks `sslmode` and `sslrootcert` sits in the CA group, so
    # leaving them behind would keep the old posture in effect.
    pool.fetchrow_result = _pg_source_row(
        {"host": "postgres", "ssl": "require", "sslrootcert": "/old/ca.crt"}
    )
    updates = _record_updates(pool)
    resp = await client.post(
        "/dashboard/data-sources/shop/edit",
        data=_tls_edit("verify-full", "/new/ca.crt"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    stored = updates[-1]
    assert "ssl" not in stored
    assert "sslrootcert" not in stored
    assert stored["sslmode"] == "verify-full"
    assert stored["ssl_ca"] == "/new/ca.crt"


@pytest.mark.anyio
async def test_source_detail_lists_only_policies_that_apply_in_evaluation_order(client, pool):
    pool.fetchrow_result = _dispatch(
        **{
            "FROM data_sources": _row(
                id=1,
                source_id="shop",
                name="Shop DB",
                source_type="postgresql",
                connection_config={"host": "postgres", "port": 5432},
                cache_strategy="deterministic_first",
                enabled=True,
                metadata={},
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        }
    )
    pool.fetch_result = _dispatch(
        **{
            "AS rule_id": [_row(rule_id="11", hits=5)],
            "FROM policy_rules": [
                _row(
                    id=11,
                    name="deny-quarantined",
                    priority=100,
                    conditions={"identity_roles": ["quarantined"]},
                    actions={"effect": "deny"},
                ),
                _row(
                    id=12,
                    name="other-source-only",
                    priority=90,
                    conditions={"source_ids": ["elsewhere"]},
                    actions={"effect": "allow"},
                ),
                _row(
                    id=13,
                    name="no-effect-given",
                    priority=50,
                    conditions={"source_id": "shop"},
                    actions={},
                ),
                _row(
                    id=14,
                    name="allow-baseline",
                    priority=1,
                    conditions={},
                    actions={"effect": "allow"},
                ),
            ],
        }
    )
    body = (await client.get("/dashboard/data-sources/shop")).text

    assert "Policies applied to this source" in body
    assert "first rule that matches decides" in body
    assert "other-source-only" not in body
    assert body.index("deny-quarantined") < body.index("no-effect-given")
    assert body.index("no-effect-given") < body.index("allow-baseline")
    assert "Denies requests from identities with the quarantined label." in body
    assert "Allows every request." in body
    # A rule with no effect is shown as the deny the engine applies.
    row = body[body.index("no-effect-given") :]
    assert 'badge-deny">deny' in row[: row.index("</tr>")]
    assert "Custom conditions" not in body
    assert '<td class="num">5</td>' in body


@pytest.mark.anyio
@pytest.mark.parametrize(("ticked", "expected"), [(True, True), (False, None)])
async def test_the_edit_page_turns_private_network_access_on_and_off(
    client, pool, ticked, expected
):
    """An unticked box sends nothing, so the page marks the field as present."""
    pool.fetchrow_result = _row(
        source_id="shop",
        name="Shop DB",
        source_type="postgresql",
        connection_config={"host": "postgres", "port": 5432, "allow_private_egress": True},
        cache_strategy="deterministic_first",
        enabled=True,
        metadata={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    updates: list[tuple] = []
    original = pool.fetchrow

    async def capture(*args, **kwargs):
        if "UPDATE data_sources" in str(args[0]):
            updates.append(args)
        return await original(*args, **kwargs)

    pool.fetchrow = capture
    data = {"name": "Shop DB", "cache_strategy": "bypass", "enabled": "on"}
    data["allow_private_egress_field"] = "1"
    if ticked:
        data["allow_private_egress"] = "on"
    resp = await client.post("/dashboard/data-sources/shop/edit", data=data, follow_redirects=False)

    assert resp.status_code == 303
    assert updates[-1][3].get("allow_private_egress") is expected
