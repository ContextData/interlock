"""Tests for the multi-step source onboarding wizard.

Covers:
  - GET /dashboard/source-wizard renders step 1.
  - POST /step2 carries source_id / name / source_type forward.
  - POST /step3 introspects (mocked) and shows the schema partial.
  - POST /step4 carries pii_columns list forward.
  - POST /step5 renders the review summary.
  - POST /save persists the source AND inserts classification_tags.
  - Save rejects duplicate source_id.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.admin.routes.dashboard import (
    _looks_like_pii,
    _wizard_state,
)
from interlock.config import InterLockConfig

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_pii_heuristic_flags_common_names() -> None:
    for name in ("email", "ssn", "credit_card", "first_name", "date_of_birth", "address"):
        assert _looks_like_pii(name)


def test_pii_heuristic_misses_neutral_names() -> None:
    for name in ("id", "amount", "created_at", "status", "qty"):
        assert not _looks_like_pii(name)


def test_wizard_state_collapses_form_strings() -> None:
    st = _wizard_state(
        {
            "source_id": "  shop  ",
            "name": " Shop ",
            "source_type": "postgresql",
            "host": "db",
            "port": "5433",
            "database": "orders",
            "user": "ro",
            "password": "secret",
            "create_default_roles": "on",
            "pii_columns": "customers.ssn",
        }
    )
    assert st["source_id"] == "shop"
    assert st["name"] == "Shop"
    assert st["port"] == "5433"
    assert st["create_default_roles"] == "on"
    assert st["pii_columns"] == ["customers.ssn"]


def test_wizard_state_handles_missing_pii_columns() -> None:
    st = _wizard_state({"source_type": "http", "base_url": "https://x"})
    assert st["pii_columns"] == []


# ---------------------------------------------------------------------------
# Route integration
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
        self.fetchval_result = None
        self.fetch_result = []

    async def fetch(self, *a, **k):
        return self.fetch_result

    async def fetchrow(self, *a, **k):
        return None

    async def fetchval(self, *a, **k):
        self._calls.append(("fetchval", a))
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
async def test_wizard_start_returns_step1(client):
    resp = await client.get("/dashboard/source-wizard")
    assert resp.status_code == 200
    body = resp.text
    assert "wizard-progress" in body
    assert 'data-step="1"' in body
    assert "Source roles" in body
    assert 'name="create_default_roles"' in body
    # Step 1 hx-post points to step2.
    assert 'hx-post="/dashboard/source-wizard/step2"' in body


@pytest.mark.anyio
async def test_wizard_start_can_render_modal_shell(client):
    resp = await client.get(
        "/dashboard/source-wizard",
        headers={"HX-Request": "true", "HX-Target": "source-modal-root"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert 'role="dialog"' in body
    assert 'id="source-wizard-modal-title"' in body
    assert 'id="new-source-form-slot"' in body
    assert 'data-step="1"' in body
    assert "Source Wizard - Onyx Dashboard" not in body


@pytest.mark.anyio
async def test_step2_requires_a_display_name(client):
    resp = await client.post(
        "/dashboard/source-wizard/step2",
        data={"name": "", "source_type": "postgresql"},
    )
    assert resp.status_code == 200
    assert "Display name is required" in resp.text
    # Re-rendered step 1 with the error.
    assert 'data-step="1"' in resp.text


@pytest.mark.anyio
async def test_step2_advances_to_connection_step(client):
    resp = await client.post(
        "/dashboard/source-wizard/step2",
        data={
            "name": "Shop",
            "source_type": "postgresql",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200
    assert 'data-step="2"' in resp.text
    # Hidden inputs carry state forward; the source id is not asked for.
    assert 'value="Shop"' in resp.text
    assert 'name="source_id"' not in resp.text
    assert 'name="create_default_roles"' in resp.text
    assert 'name="host"' in resp.text


@pytest.mark.anyio
async def test_wizard_test_button_runs_probe(client):
    resp = await client.post(
        "/dashboard/source-wizard/test",
        data={
            "source_type": "postgresql",
            "host": "127.0.0.1",
            "port": "1",
            "database": "",
            "user": "",
            "password": "",
            "base_url": "",
        },
    )
    assert resp.status_code == 200
    # Either failed (expected) or unexpectedly succeeded - both render
    # the result strip.
    body = resp.text
    assert ("Connection failed" in body) or ("Connected in" in body)


@pytest.mark.anyio
async def test_step3_introspect_falls_through_on_error(client):
    # No PG to connect to (port 1) - schema introspection fails gracefully.
    resp = await client.post(
        "/dashboard/source-wizard/step3",
        data={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "host": "127.0.0.1",
            "port": "1",
            "database": "x",
            "user": "u",
            "password": "p",
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert 'data-step="3"' in body
    assert "Could not introspect" in body or "Schema preview" in body


@pytest.mark.anyio
async def test_step3_skips_introspect_for_http_source(client):
    resp = await client.post(
        "/dashboard/source-wizard/step3",
        data={
            "source_id": "api",
            "name": "API",
            "source_type": "http",
            "base_url": "https://api.example.com",
        },
    )
    assert resp.status_code == 200
    assert "only available for PostgreSQL" in resp.text


@pytest.mark.anyio
async def test_step4_carries_pii_columns_forward(client):
    resp = await client.post(
        "/dashboard/source-wizard/step4",
        data={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "host": "db",
            "port": "5432",
            "database": "shop",
            "user": "",
            "password": "",
            "pii_columns": ["customers.ssn", "customers.email"],
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert 'data-step="4"' in body
    # The hidden inputs render one per checked column.
    assert body.count("value='customers.ssn'") >= 1
    assert body.count("value='customers.email'") >= 1


@pytest.mark.anyio
async def test_step5_review_renders_summary(client):
    resp = await client.post(
        "/dashboard/source-wizard/step5",
        data={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "host": "db",
            "port": "5432",
            "database": "shop",
            "cache_strategy": "deterministic_first",
            "create_default_roles": "on",
            "pii_columns": ["customers.ssn"],
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert "Review and save" in body
    assert "db:5432/shop" in body
    assert "default IAM-style templates" in body
    assert "customers.ssn" in body


@pytest.mark.anyio
async def test_source_wizard_does_not_render_hidden_raw_secrets(client):
    step3 = await client.post(
        "/dashboard/source-wizard/step3",
        data={
            "wizard_id": "wiz-secret",
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "host": "db",
            "port": "5432",
            "database": "shop",
            "user": "reader",
            "password": "super-secret-password",
        },
    )
    assert step3.status_code == 200
    assert "super-secret-password" not in step3.text
    assert 'name="password"' not in step3.text

    step5 = await client.post(
        "/dashboard/source-wizard/step5",
        data={
            "wizard_id": "wiz-secret",
            "cache_strategy": "deterministic_first",
        },
    )
    assert step5.status_code == 200
    assert "super-secret-password" not in step5.text
    assert "inline password supplied" in step5.text
    assert 'name="password"' not in step5.text


@pytest.mark.anyio
async def test_source_wizard_cancel_clears_server_draft(client, app):
    await client.post(
        "/dashboard/source-wizard/step2",
        data={
            "wizard_id": "wiz-cancel",
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "password": "super-secret-password",
        },
    )
    assert app.state._wizard_drafts
    resp = await client.post(
        "/dashboard/source-wizard/cancel",
        data={"wizard_id": "wiz-cancel"},
        follow_redirects=False,
    )
    assert resp.status_code in {200, 303}
    assert not app.state._wizard_drafts


@pytest.mark.anyio
async def test_save_persists_source_and_classifications(client, pool):
    pool.fetchval_result = None  # no duplicate
    resp = await client.post(
        "/dashboard/source-wizard/save",
        data={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "host": "postgres",
            "port": "5432",
            "database": "shop",
            "user": "",
            "password": "",
            "cache_strategy": "deterministic_first",
            "create_default_roles": "on",
            "pii_columns": ["customers.ssn", "orders.email"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/data-sources/shop"
    inserts = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserts
    metadata = inserts[-1][1][6]
    assert "source_roles_version" in metadata
    role_inserts = [c for c in pool._calls if "INSERT INTO source_roles" in c[1][0]]
    assert role_inserts
    # Wizard PII flags land in the source catalog's annotations, keyed by the
    # full catalog path, so a rescan never erases them.
    tags = [c for c in pool._calls if "INSERT INTO source_catalog_annotations" in c[1][0]]
    assert len(tags) == 2
    assert [c[1][2] for c in tags] == [
        ["public", "customers", "ssn"],
        ["public", "orders", "email"],
    ]
    # The actual columns went through.
    flat = " ".join(str(c[1]) for c in tags)
    assert "ssn" in flat and "email" in flat


@pytest.mark.anyio
async def test_save_rejects_duplicate_source(client, pool):
    pool.fetchval_result = 1
    resp = await client.post(
        "/dashboard/source-wizard/save",
        data={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "host": "postgres",
            "port": "5432",
            "database": "shop",
            "cache_strategy": "deterministic_first",
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert "already exists" in body
    inserts = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert not inserts


# ---------------------------------------------------------------------------
# Upstream TLS: the wizard can set it, and refuses what production refuses
# ---------------------------------------------------------------------------

_TLS_REQUIRED = "Verified upstream PostgreSQL TLS is required"


@pytest.fixture
async def production_client(app):
    app.state.config = app.state.config.model_copy(update={"environment": "production"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _wizard_pg(**extra):
    return {
        "source_id": "shop",
        "name": "Shop",
        "source_type": "postgresql",
        "connector_key": "postgresql",
        "host": "db.example.com",
        "port": "5432",
        "database": "shop",
        "user": "ro",
        "password_ref": "env://SHOP_PASSWORD",
        "cache_strategy": "deterministic_first",
        **extra,
    }


def _inserted_configs(pool):
    return [c[1][4] for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]


def test_wizard_state_carries_upstream_tls_fields() -> None:
    st = _wizard_state({"source_type": "postgresql", "sslmode": "verify-ca", "ssl_ca": "/ca.crt"})
    assert st["sslmode"] == "verify-ca"
    assert st["ssl_ca"] == "/ca.crt"


@pytest.mark.anyio
async def test_wizard_connection_step_offers_tls_fields_for_postgresql_only(client):
    pg = await client.post("/dashboard/source-wizard/step2", data=_wizard_pg())
    assert 'name="sslmode"' in pg.text
    assert 'name="ssl_ca"' in pg.text
    mysql = await client.post(
        "/dashboard/source-wizard/step2",
        data={**_wizard_pg(), "source_type": "mysql", "connector_key": "mysql"},
    )
    assert 'name="host"' in mysql.text
    assert 'name="sslmode"' not in mysql.text


@pytest.mark.anyio
@pytest.mark.parametrize("tls", [{}, {"sslmode": "require"}], ids=["no-mode", "require"])
async def test_production_wizard_save_refuses_unverified_postgresql_tls(
    production_client, pool, tls
):
    pool.fetchval_result = None
    resp = await production_client.post(
        "/dashboard/source-wizard/save", data=_wizard_pg(**tls), follow_redirects=False
    )
    assert resp.status_code == 200
    assert 'data-step="5"' in resp.text
    assert _TLS_REQUIRED in resp.text
    assert _inserted_configs(pool) == []


@pytest.mark.anyio
async def test_production_wizard_save_stores_verified_postgresql_tls(production_client, pool):
    pool.fetchval_result = None
    resp = await production_client.post(
        "/dashboard/source-wizard/save",
        data=_wizard_pg(sslmode="verify-full", ssl_ca="/run/secrets/shop-ca/ca.crt"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cfg = _inserted_configs(pool)[-1]
    assert cfg["sslmode"] == "verify-full"
    assert cfg["ssl_ca"] == "/run/secrets/shop-ca/ca.crt"


@pytest.mark.anyio
async def test_wizard_review_step_carries_tls_fields_to_save(client):
    resp = await client.post(
        "/dashboard/source-wizard/step5", data=_wizard_pg(sslmode="verify-ca", ssl_ca="/ca.crt")
    )
    assert 'name="sslmode" value="verify-ca"' in resp.text
    assert 'name="ssl_ca" value="/ca.crt"' in resp.text


@pytest.mark.anyio
async def test_production_wizard_test_probes_with_the_tls_fields(production_client):
    from datetime import UTC, datetime

    from interlock.models import HealthStatus

    probe = AsyncMock(
        return_value=HealthStatus(
            source_id="<unsaved>",
            healthy=False,
            latency_ms=0.0,
            error="probe stubbed",
            checked_at=datetime.now(UTC),
        )
    )
    with patch("interlock.admin.routes.dashboard.ConnectionManager.probe_unsaved", probe):
        await production_client.post(
            "/dashboard/source-wizard/test", data=_wizard_pg(sslmode="verify-ca", ssl_ca="/ca.crt")
        )
    cfg = probe.call_args.args[1]
    assert cfg["sslmode"] == "verify-ca"
    assert cfg["ssl_ca"] == "/ca.crt"
    assert probe.call_args.kwargs["allow_insecure_tls"] is False


@pytest.mark.anyio
async def test_production_wizard_introspection_refuses_unverified_tls_without_connecting(
    production_client,
):
    connect = AsyncMock(side_effect=AssertionError("introspection must not connect"))
    with patch("asyncpg.connect", connect):
        resp = await production_client.post(
            "/dashboard/source-wizard/step3", data=_wizard_pg(sslmode="require")
        )
    assert resp.status_code == 200
    assert _TLS_REQUIRED in resp.text
    connect.assert_not_called()


@pytest.mark.anyio
async def test_wizard_introspection_connects_with_the_configured_tls(client):
    connect = AsyncMock(side_effect=OSError("unreachable"))
    with patch("asyncpg.connect", connect):
        await client.post("/dashboard/source-wizard/step3", data=_wizard_pg(sslmode="require"))
    assert connect.call_args.kwargs["ssl"] == "require"


def test_wizard_pii_paths_keep_dotted_identifiers_whole() -> None:
    from interlock.admin.routes.dashboard import _wizard_pii_paths

    assert _wizard_pii_paths(
        [
            '["sales","customers","email"]',
            '["sales","odd.table","ssn"]',
            "customers.phone",
            "hr.employees.salary",
            '["sales","customers","email"]',
            "not-a-path",
            "[broken",
            '["too","short"]',
        ]
    ) == [
        ["sales", "customers", "email"],
        ["sales", "odd.table", "ssn"],
        ["public", "customers", "phone"],
        ["hr", "employees", "salary"],
    ]


@pytest.mark.anyio
async def test_save_generates_the_source_id_from_the_display_name(client, pool):
    resp = await client.post(
        "/dashboard/source-wizard/save",
        data={
            "name": "Sales PostgreSQL",
            "source_type": "postgresql",
            "host": "postgres",
            "port": "5432",
            "database": "shop",
            "cache_strategy": "deterministic_first",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/data-sources/sales_postgresql"
    inserts = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserts[-1][1][1] == "sales_postgresql"


@pytest.mark.anyio
async def test_review_step_shows_the_id_that_will_be_generated(client):
    resp = await client.post(
        "/dashboard/source-wizard/step5",
        data={"name": "HR MySQL", "source_type": "mysql", "connector_key": "mysql"},
    )
    assert resp.status_code == 200
    assert "<code>hr_mysql</code>" in resp.text
    assert 'name="source_id"' not in resp.text


@pytest.mark.anyio
async def test_the_wizard_can_opt_a_source_into_a_private_network(client, pool):
    pool.fetchval_result = None
    resp = await client.post(
        "/dashboard/source-wizard/save",
        data={
            "name": "Sample shop",
            "source_type": "postgresql",
            "host": "sample-postgres",
            "port": "5432",
            "database": "shop",
            "sslmode": "disable",
            "cache_strategy": "deterministic_first",
            "allow_private_egress": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    inserts = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserts[-1][1][4]["allow_private_egress"] is True
