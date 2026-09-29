"""Unit tests for the Admin API CRUD endpoints."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
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


def test_admin_observability_bootstrap_is_wired() -> None:
    cfg = _make_config()
    otel_result = MagicMock()

    with patch("interlock.admin.app.configure_service_otel", return_value=otel_result) as bootstrap:
        app = create_app(config=cfg)

    bootstrap.assert_called_once_with(
        service_name="interlock-admin",
        config=cfg,
        app=app,
    )
    assert app.state.otel is otel_result


def _fake_record(data: dict):
    """Create a fake asyncpg.Record-like object (supports dict() and key access)."""
    rec = MagicMock()
    rec.__iter__ = MagicMock(return_value=iter(data.items()))
    rec.__getitem__ = MagicMock(side_effect=lambda k: data[k])
    rec.keys = MagicMock(return_value=data.keys())
    rec.values = MagicMock(return_value=data.values())
    rec.items = MagicMock(return_value=data.items())
    # Make dict(record) work
    # asyncpg.Record supports dict() conversion
    rec.__class__ = type(
        "FakeRecord",
        (),
        {
            "__iter__": lambda self: iter(data.keys()),
            "__getitem__": lambda self, k: data[k],
            "keys": lambda self: data.keys(),
            "values": lambda self: data.values(),
            "items": lambda self: data.items(),
        },
    )
    # Override for dict() builtin
    original_data = dict(data)

    class FakeRecord:
        def keys(self):
            return original_data.keys()

        def values(self):
            return original_data.values()

        def items(self):
            return original_data.items()

        def __iter__(self):
            return iter(original_data.keys())

        def __getitem__(self, key):
            return original_data[key]

    return FakeRecord()


NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)

SAMPLE_DS = {
    "id": 1,
    "source_id": "warehouse",
    "name": "Data Warehouse",
    "source_type": "postgresql",
    "connection_config": json.dumps({"host": "db.example.com"}),
    "cache_strategy": "deterministic_first",
    "enabled": True,
    "metadata": json.dumps({}),
    "created_at": NOW,
    "updated_at": NOW,
}

SAMPLE_POLICY = {
    "id": 1,
    "name": "deny-drops",
    "priority": 100,
    "conditions": json.dumps({"operation": "DROP"}),
    "actions": json.dumps({"effect": "deny"}),
    "enabled": True,
    "created_at": NOW,
}

SAMPLE_IDENTITY = {
    "id": 1,
    "name": "test-agent",
    "api_key_hash": hashlib.sha256(b"secret-key-123").hexdigest(),
    "agent_type": "claude_code",
    "team": "platform",
    "roles": ["reader"],
    "mapped_pg_role": None,
    "enabled": True,
    "metadata": json.dumps({}),
    "created_at": NOW,
    "updated_at": NOW,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeConnection:
    """Mock asyncpg connection with configurable return values."""

    def __init__(self):
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=None)
        self.execute = AsyncMock(return_value="DELETE 1")

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


@pytest.fixture
def mock_pool():
    return FakePool()


@pytest.fixture
def app(mock_pool):
    """Create app with mocked infrastructure - bypass lifespan and set state directly."""
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
        # Pre-set state so tests work even if lifespan doesn't fully run
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
# Health
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "admin"


# ---------------------------------------------------------------------------
# Data Sources
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_data_sources(client, mock_pool):
    mock_pool.conn.fetch.return_value = [_fake_record(SAMPLE_DS)]
    resp = await client.get("/api/data-sources")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["source_id"] == "warehouse"


@pytest.mark.anyio
async def test_get_data_source(client, mock_pool):
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)
    resp = await client.get("/api/data-sources/warehouse")
    assert resp.status_code == 200
    assert resp.json()["source_id"] == "warehouse"


@pytest.mark.anyio
async def test_get_data_source_not_found(client, mock_pool):
    mock_pool.conn.fetchrow.return_value = None
    resp = await client.get("/api/data-sources/nonexistent")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_create_data_source(client, mock_pool):
    mock_pool.conn.fetchval.return_value = None  # no duplicate
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)
    resp = await client.post(
        "/api/data-sources",
        json={
            "source_id": "warehouse",
            "name": "Data Warehouse",
            "source_type": "postgresql",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["source_id"] == "warehouse"
    # Verify NOTIFY was called (acquire called for insert + notify)
    assert mock_pool.conn.execute.await_count >= 1


@pytest.mark.anyio
async def test_create_data_source_duplicate(client, mock_pool):
    mock_pool.conn.fetchval.return_value = 1  # duplicate exists
    resp = await client.post(
        "/api/data-sources",
        json={
            "source_id": "warehouse",
            "name": "Data Warehouse",
            "source_type": "postgresql",
        },
    )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_update_data_source(client, mock_pool):
    before = dict(SAMPLE_DS)
    updated = dict(SAMPLE_DS)
    updated["name"] = "Updated Warehouse"
    mock_pool.conn.fetchrow.side_effect = [_fake_record(before), _fake_record(updated)]
    resp = await client.put(
        "/api/data-sources/warehouse",
        json={"name": "Updated Warehouse"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Warehouse"


@pytest.mark.anyio
async def test_update_data_source_writes_redacted_admin_audit(client, mock_pool):
    before = dict(SAMPLE_DS)
    before["connection_config"] = json.dumps(
        {"host": "db.example.com", "password": "old-secret", "allow_private_egress": True}
    )
    updated = dict(SAMPLE_DS)
    updated["connection_config"] = json.dumps(
        {
            "host": "db.example.com",
            "password": "new-secret",
            "database": "app",
            "allow_private_egress": True,
        }
    )
    mock_pool.conn.fetchrow.side_effect = [_fake_record(before), _fake_record(updated)]

    resp = await client.put(
        "/api/data-sources/warehouse",
        json={
            "connection_config": {
                "host": "db.example.com",
                "password": "new-secret",
                "allow_private_egress": True,
            }
        },
    )

    assert resp.status_code == 200
    audit_args = mock_pool.execute.await_args.args
    assert audit_args[3] == "data_source.update"
    assert audit_args[4] == "data_source"
    detail = audit_args[6]
    assert detail["before"]["connection_config"]["password"] == "[REDACTED]"
    assert detail["after"]["connection_config"]["password"] == "[REDACTED]"
    assert detail["after"]["connection_config"]["database"] == "app"
    assert "new-secret" not in str(detail)
    assert detail["changed_fields"] == ["connection_config"]


@pytest.mark.anyio
async def test_delete_data_source(client, mock_pool):
    mock_pool.conn.execute.return_value = "DELETE 1"
    resp = await client.delete("/api/data-sources/warehouse")
    assert resp.status_code == 204


@pytest.mark.anyio
async def test_delete_data_source_not_found(client, mock_pool):
    mock_pool.conn.execute.return_value = "DELETE 0"
    resp = await client.delete("/api/data-sources/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_identity_hashes_key(client, mock_pool):
    raw_key = "secret-key-1234567890-ABCDEFGHijklmnop"
    mock_pool.conn.fetchval.return_value = None  # no duplicate
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_IDENTITY)
    resp = await client.post(
        "/api/identities",
        json={
            "name": "test-agent",
            "api_key": raw_key,
            "agent_type": "claude_code",
            "team": "platform",
            "roles": ["reader"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    # Verify api_key_hash is NOT in the response
    assert "api_key_hash" not in data
    assert data["name"] == "test-agent"

    # Verify the hash was passed to the DB insert
    insert_call = mock_pool.conn.fetchrow.await_args
    args = insert_call[0]
    expected_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    assert expected_hash in args


@pytest.mark.anyio
async def test_create_identity_writes_redacted_admin_audit(client, mock_pool):
    raw_key = "secret-key-1234567890-ABCDEFGHijklmnop"
    identity = dict(SAMPLE_IDENTITY)
    identity["metadata"] = json.dumps({"token": "super-secret-token", "purpose": "e2e"})
    mock_pool.conn.fetchval.return_value = None
    mock_pool.conn.fetchrow.return_value = _fake_record(identity)

    resp = await client.post(
        "/api/identities",
        json={
            "name": "test-agent",
            "api_key": raw_key,
            "agent_type": "claude_code",
            "team": "platform",
            "roles": ["reader"],
            "metadata": {"token": "super-secret-token", "purpose": "e2e"},
        },
    )

    assert resp.status_code == 201
    audit_args = mock_pool.execute.await_args.args
    assert audit_args[3] == "identity.create"
    assert audit_args[4] == "identity"
    detail = audit_args[6]
    assert detail["after"]["metadata"]["token"] == "[REDACTED]"
    assert detail["after"]["metadata"]["purpose"] == "e2e"
    assert "api_key_hash" not in str(detail)
    assert raw_key not in str(detail)


@pytest.mark.anyio
async def test_list_identities_excludes_hash(client, mock_pool):
    mock_pool.conn.fetch.return_value = [_fake_record(SAMPLE_IDENTITY)]
    resp = await client.get("/api/identities")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert "api_key_hash" not in data[0]
    assert data[0]["name"] == "test-agent"


@pytest.mark.anyio
async def test_get_identity(client, mock_pool):
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_IDENTITY)
    resp = await client.get("/api/identities/1")
    assert resp.status_code == 200
    assert "api_key_hash" not in resp.json()


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_policy_with_notify(client, mock_pool):
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_POLICY)
    resp = await client.post(
        "/api/policies",
        json={
            "name": "deny-drops",
            "priority": 100,
            "conditions": {"operation": "DROP"},
            "actions": {"effect": "deny"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "deny-drops"
    # Verify NOTIFY was called
    execute_calls = mock_pool.conn.execute.await_args_list
    notify_found = any("NOTIFY onyx_policy_changed" in str(c) for c in execute_calls)
    assert notify_found, f"NOTIFY not found in execute calls: {execute_calls}"


@pytest.mark.anyio
async def test_list_policies(client, mock_pool):
    mock_pool.conn.fetch.return_value = [_fake_record(SAMPLE_POLICY)]
    resp = await client.get("/api/policies")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.anyio
async def test_get_policy(client, mock_pool):
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_POLICY)
    resp = await client.get("/api/policies/1")
    assert resp.status_code == 200
    assert resp.json()["name"] == "deny-drops"


@pytest.mark.anyio
async def test_update_policy(client, mock_pool):
    before = dict(SAMPLE_POLICY)
    updated = dict(SAMPLE_POLICY)
    updated["priority"] = 200
    mock_pool.conn.fetchrow.side_effect = [_fake_record(before), _fake_record(updated)]
    resp = await client.put("/api/policies/1", json={"priority": 200})
    assert resp.status_code == 200
    assert resp.json()["priority"] == 200
    audit_args = mock_pool.execute.await_args_list[-1].args
    assert audit_args[3] == "policy.update"
    assert audit_args[4] == "policy_rule"
    detail = audit_args[6]
    assert detail["before"]["priority"] == 100
    assert detail["after"]["priority"] == 200
    assert detail["changed_fields"] == ["priority"]


@pytest.mark.anyio
async def test_delete_policy(client, mock_pool):
    mock_pool.conn.execute.return_value = "DELETE 1"
    resp = await client.delete("/api/policies/1")
    assert resp.status_code == 204


@pytest.mark.anyio
async def test_create_data_source_accepts_the_libpq_ca_spelling(client, mock_pool):
    """The exact request that answered 422 on the live deployment.

    `sslrootcert` is the libpq parameter name and what appears in every
    PostgreSQL connection string, and `read_connection_field` has always
    honoured it at connect time. The strict allowlist was maintained
    separately from the alias table, so the API refused to store the spelling
    an operator is most likely to type while accepting its synonym.
    """
    mock_pool.conn.fetchval.return_value = None
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)

    resp = await client.post(
        "/api/data-sources",
        json={
            "source_id": "warehouse",
            "name": "Data Warehouse",
            "source_type": "postgresql",
            "connection_config": {
                "host": "db.example.com",
                "allow_private_egress": True,
                "port": 25060,
                "database": "sales",
                "user": "reader",
                "password_ref": "env://PGPASSWORD",
                "sslmode": "verify-full",
                "sslrootcert": "/run/secrets/db-ca/ca.crt",
            },
        },
    )

    assert resp.status_code == 201, resp.text


@pytest.mark.anyio
async def test_create_data_source_still_rejects_an_unknown_field(client, mock_pool):
    """Widening the allowlist to cover aliases must not disable strict mode."""
    mock_pool.conn.fetchval.return_value = None
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)

    resp = await client.post(
        "/api/data-sources",
        json={
            "source_id": "warehouse",
            "name": "Data Warehouse",
            "source_type": "postgresql",
            "connection_config": {"sslrootcrt": "/typo.crt"},
        },
    )

    assert resp.status_code == 422
    assert "sslrootcrt" in resp.text


# ---------------------------------------------------------------------------
# Source-role grants
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_granting_a_source_does_not_touch_the_api_key(client, mock_pool):
    """The whole point of #76: an agent gains a source without being re-keyed.

    Until these routes existed, the only way to give an existing identity
    access to a newly registered source was to delete it and recreate it,
    which mints a new key and breaks every deployed client holding the old one.
    """
    mock_pool.conn.fetchval.return_value = 1  # the identity exists
    mock_pool.conn.fetchrow.side_effect = [
        {"id": 5, "role_key": "reader", "source_id": "sales_pg"},  # role resolution
        {
            "id": 77,
            "identity_id": 3,
            "source_id": "sales_pg",
            "role_id": 5,
            "enabled": True,
            "expires_at": None,
            "granted_by": None,
            "created_at": None,
            "updated_at": None,
        },
    ]

    resp = await client.post(
        "/api/identities/3/source-role-grants",
        json={"source_id": "sales_pg", "role_key": "reader"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_id"] == "sales_pg"
    assert body["role_key"] == "reader"
    assert body["enabled"] is True
    # Nothing in the response or the request concerns the credential.
    assert "api_key" not in resp.text


@pytest.mark.anyio
async def test_granting_a_role_from_another_source_is_refused(client, mock_pool):
    """Such a grant would store cleanly and never match, reading as success."""
    mock_pool.conn.fetchval.return_value = 1
    mock_pool.conn.fetchrow.return_value = {
        "id": 5,
        "role_key": "reader",
        "source_id": "hr_mysql",
    }

    resp = await client.post(
        "/api/identities/3/source-role-grants",
        json={"source_id": "sales_pg", "role_id": 5},
    )

    assert resp.status_code == 422
    assert "hr_mysql" in resp.text


@pytest.mark.anyio
async def test_granting_on_an_unknown_identity_is_a_404(client, mock_pool):
    mock_pool.conn.fetchval.return_value = None

    resp = await client.post(
        "/api/identities/999/source-role-grants",
        json={"source_id": "sales_pg", "role_key": "reader"},
    )

    assert resp.status_code == 404


@pytest.mark.anyio
async def test_revoking_keeps_the_row_and_reports_it_disabled(client, mock_pool):
    """A deleted row cannot answer whether the agent ever held access."""
    mock_pool.conn.fetchval.return_value = 1
    mock_pool.conn.fetchrow.return_value = {
        "id": 77,
        "identity_id": 3,
        "source_id": "sales_pg",
        "role_id": 5,
        "role_key": "reader",
        "enabled": False,
        "expires_at": None,
        "granted_by": 1,
        "created_at": None,
        "updated_at": None,
    }

    resp = await client.delete("/api/identities/3/source-role-grants/77")

    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False


@pytest.mark.anyio
async def test_revoking_a_grant_the_identity_does_not_have_is_a_404(client, mock_pool):
    mock_pool.conn.fetchval.return_value = 1
    mock_pool.conn.fetchrow.return_value = None

    resp = await client.delete("/api/identities/3/source-role-grants/1234")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Upstream TLS is judged at registration, not only at the first query
# ---------------------------------------------------------------------------


@pytest.fixture
def production_app(mock_pool):
    """The same app with a production config, which is what the TLS gate reads."""
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as mock_create_pg,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch("interlock.admin.app.create_redis_client", new_callable=AsyncMock),
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        mock_create_pg.return_value = mock_pool
        application = create_app(config=_make_config())
        application.state.config = _make_config().model_copy(update={"environment": "production"})
        application.state.pg_pool = mock_pool
        application.state.redis = MagicMock()
        application.state.auth_disabled = True
        yield application


@pytest.fixture
async def production_client(production_app):
    async with AsyncClient(
        transport=ASGITransport(app=production_app), base_url="http://test"
    ) as c:
        yield c


def _pg_source(**tls):
    return {
        "source_id": "warehouse",
        "name": "Data Warehouse",
        "source_type": "postgresql",
        "connector_key": "postgresql",
        "connection_config": {
            "host": "db.example.com",
            "allow_private_egress": True,
            "port": 5432,
            "database": "sales",
            "user": "reader",
            "password_ref": "env://PGPASSWORD",
            **tls,
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tls",
    [
        {},
        {"sslmode": "disable"},
        {"sslmode": "require"},
        {"sslmode": "verify-full", "verify_ssl": False},
    ],
    ids=["no-mode", "disable", "require", "verification-off"],
)
async def test_production_refuses_to_register_an_unverified_postgresql_source(
    production_client, mock_pool, tls
):
    """Refused at save, with the gate's own words.

    Storing a configuration production will never connect with moves the
    failure to the first agent query, where no operator is looking. On the live
    deployment the API accepted exactly such a source with 201.
    """
    mock_pool.conn.fetchval.return_value = None
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)

    resp = await production_client.post("/api/data-sources", json=_pg_source(**tls))

    assert resp.status_code == 422, resp.text
    assert "upstream PostgreSQL TLS" in resp.json()["detail"]
    mock_pool.conn.fetchrow.assert_not_awaited()


@pytest.mark.anyio
async def test_production_registers_a_verified_postgresql_source(production_client, mock_pool):
    mock_pool.conn.fetchval.return_value = None
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)

    resp = await production_client.post(
        "/api/data-sources",
        json=_pg_source(sslmode="verify-full", ssl_ca="/run/secrets/db-ca/ca.crt"),
    )

    assert resp.status_code == 201, resp.text


@pytest.mark.anyio
async def test_development_still_registers_an_unverified_postgresql_source(client, mock_pool):
    """A local server with no TLS stays usable outside production."""
    mock_pool.conn.fetchval.return_value = None
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)

    resp = await client.post("/api/data-sources", json=_pg_source(sslmode="require"))

    assert resp.status_code == 201, resp.text


@pytest.mark.anyio
async def test_production_refuses_to_update_a_source_into_an_unverified_posture(
    production_client, mock_pool
):
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)
    body = _pg_source(sslmode="require")
    del body["source_id"]

    resp = await production_client.put("/api/data-sources/warehouse", json=body)

    assert resp.status_code == 422, resp.text
    assert "upstream PostgreSQL TLS" in resp.json()["detail"]


@pytest.mark.anyio
async def test_production_test_connection_refuses_without_touching_the_network(production_client):
    """Test Connection and Save must give the same answer."""
    connect = AsyncMock()
    with patch("interlock.connections.manager.asyncpg.connect", connect):
        resp = await production_client.post(
            "/api/data-sources/test",
            json={
                "source_type": "postgresql",
                "connector_key": "postgresql",
                "connection_config": _pg_source(sslmode="require")["connection_config"],
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert resp.json()["error"] == "Verified upstream PostgreSQL TLS is required"
    connect.assert_not_called()


@pytest.mark.anyio
async def test_delete_identity_leaves_a_tombstone_before_removing_the_row(client, mock_pool):
    mock_pool.conn.fetchrow.return_value = {
        "id": 7,
        "name": "analyst-claude",
        "team": "rehearsal",
        "agent_type": "claude_code",
        "roles": [],
        "mapped_pg_role": None,
        "enabled": True,
        "metadata": {},
    }

    resp = await client.delete("/api/identities/7")

    assert resp.status_code == 204
    statements = [call.args[0] for call in mock_pool.conn.execute.await_args_list]
    tombstone = next(i for i, sql in enumerate(statements) if "identity_tombstones" in sql)
    delete = next(i for i, sql in enumerate(statements) if "DELETE FROM identities" in sql)
    assert tombstone < delete
    assert mock_pool.conn.execute.await_args_list[tombstone].args[1:4] == (
        7,
        "analyst-claude",
        "rehearsal",
    )


@pytest.mark.anyio
async def test_delete_missing_identity_is_404_and_writes_nothing(client, mock_pool):
    mock_pool.conn.fetchrow.return_value = None

    resp = await client.delete("/api/identities/404")

    assert resp.status_code == 404
    assert mock_pool.conn.execute.await_count == 0


@pytest.mark.anyio
async def test_api_create_generates_the_source_id_when_omitted(client, mock_pool):
    mock_pool.conn.fetch.return_value = [{"source_id": "warehouse_db"}]
    mock_pool.conn.fetchrow.return_value = _fake_record(SAMPLE_DS)
    body = _pg_source(sslmode="require")
    del body["source_id"]
    body["name"] = "Warehouse DB"

    resp = await client.post("/api/data-sources", json=body)

    assert resp.status_code == 201, resp.text
    insert = mock_pool.conn.fetchrow.await_args
    assert insert.args[1] == "warehouse_db_2"


@pytest.mark.anyio
async def test_api_create_refuses_a_malformed_source_id(client, mock_pool):
    body = _pg_source(sslmode="require")
    body["source_id"] = "Not Valid"

    resp = await client.post("/api/data-sources", json=body)

    assert resp.status_code == 422
    assert "source_id must be" in resp.json()["detail"]
    mock_pool.conn.fetchrow.assert_not_awaited()
