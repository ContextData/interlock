"""Admin production-MVP UX route regressions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.config import InterLockConfig


class FakeConn:
    def __init__(self) -> None:
        self.statements: list[tuple[object, ...]] = []

    async def execute(self, *args, **kwargs):
        self.statements.append(args)
        return "OK"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        return self


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConn()
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=None)
        self.execute = AsyncMock(return_value="OK")

    def acquire(self):
        return self.connection


@pytest.fixture
def app():
    pool = FakePool()
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
        application = create_app(config=InterLockConfig())
    application.state.pg_pool = pool
    application.state.redis = redis
    application.state.auth_disabled = True
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "heading"),
    [
        ("/dashboard/source-wizard", "Source Wizard"),
        ("/dashboard/access-control/identities/new", "New Identity"),
        ("/dashboard/policies/new", "New Policy"),
        ("/dashboard/alerts/new", "New Alert Rule"),
    ],
)
async def test_direct_form_routes_render_full_shell(client, path, heading):
    resp = await client.get(path)
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" in resp.text
    assert '<nav class="sidebar"' in resp.text
    assert heading in resp.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "marker"),
    [
        ("/dashboard/source-wizard", 'data-step="1"'),
        ("/dashboard/access-control/identities/new", 'id="new-identity-form"'),
        ("/dashboard/policies/new", 'id="new-policy-form"'),
        ("/dashboard/alerts/new", 'id="alert-form"'),
    ],
)
async def test_inner_htmx_form_routes_render_fragments(client, path, marker):
    resp = await client.get(path, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" not in resp.text
    assert '<nav class="sidebar"' not in resp.text
    assert marker in resp.text


@pytest.mark.anyio
async def test_connectors_main_content_htmx_does_not_render_nested_shell(client):
    resp = await client.get(
        "/dashboard/connectors",
        headers={"HX-Request": "true", "HX-Target": "main-content"},
    )
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" not in resp.text
    assert '<nav class="sidebar"' not in resp.text
    assert 'class="layout"' not in resp.text
    assert "<h1>Connectors</h1>" in resp.text


@pytest.mark.anyio
async def test_admin_shell_has_logout_control_and_aria_current(client):
    resp = await client.get("/dashboard/overview")
    assert resp.status_code == 200
    assert 'hx-post="/auth/logout"' in resp.text
    assert 'aria-current="page"' in resp.text


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/dashboard/categories", "/dashboard/entities"])
async def test_discovery_child_pages_mark_discovery_navigation_current(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200
    assert 'href="/dashboard/discovery" class="active" aria-current="page"' in resp.text


@pytest.mark.anyio
async def test_policy_import_preview_is_non_mutating(client, app):
    payload = '{"policies":[{"name":"readers","conditions":{"roles":["read"]},"actions":{"effect":"allow"}}]}'
    resp = await client.post(
        "/dashboard/policies/import/preview",
        data={"policy_payload": payload},
    )
    assert resp.status_code == 200
    assert "Previewed 1 policy rule" in resp.text
    app.state.pg_pool.execute.assert_not_called()


@pytest.mark.anyio
async def test_policy_import_apply_uses_apply_endpoint(client, app):
    payload = '{"policies":[{"name":"readers","conditions":{"roles":["read"]},"actions":{"effect":"allow"}}]}'
    resp = await client.post(
        "/dashboard/policies/import/apply",
        data={"policy_payload": payload},
    )
    assert resp.status_code == 200
    assert "Applied 1 policy rule" in resp.text
    statements = " ".join(str(item) for item in app.state.pg_pool.connection.statements)
    assert "INSERT INTO policy_rules" in statements
    assert "NOTIFY onyx_policy_changed" in statements
