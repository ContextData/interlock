"""Unit tests for admin middleware (rate limiter, security headers)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from interlock.admin.auth import CSRF_REDIS_PREFIX, SESSION_REDIS_PREFIX, sign_cookie
from interlock.admin.auth_middleware import AdminAuthMiddleware
from interlock.admin.middleware import AdminRateLimitMiddleware, SecurityHeadersMiddleware

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(
    max_requests: int = 5,
    window_seconds: int = 60,
) -> FastAPI:
    """Create a minimal FastAPI app with both middlewares."""
    app = FastAPI()

    # Order matters: security headers wrap rate limiter wrap routes
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        AdminRateLimitMiddleware,
        max_requests=max_requests,
        window_seconds=window_seconds,
    )

    @app.get("/test")
    async def test_endpoint():
        return {"ok": True}

    @app.get("/health")
    async def health_endpoint():
        return {"ok": True}

    @app.post("/auth/login")
    async def login_endpoint():
        return {"ok": True}

    return app


@pytest.fixture
def app():
    return _make_app(max_requests=5, window_seconds=60)


@pytest.fixture
def client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------


class TestAdminRateLimitMiddleware:
    @pytest.mark.asyncio
    async def test_allows_non_login_navigation_over_limit(self, client):
        for _ in range(20):
            resp = await client.get("/test")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_allows_login_under_limit(self, client):
        for _ in range(5):
            resp = await client.post("/auth/login")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_blocks_login_over_limit(self, client):
        # Exhaust the limit
        for _ in range(5):
            await client.post("/auth/login")

        # Next request should be blocked
        resp = await client.post("/auth/login")
        assert resp.status_code == 429
        assert "rate limit" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_health_does_not_consume_login_limit(self, client):
        for _ in range(10):
            resp = await client.get("/health")
            assert resp.status_code == 200

        resp = await client.post("/auth/login")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Security headers tests
# ---------------------------------------------------------------------------


class TestSecurityHeadersMiddleware:
    @pytest.mark.asyncio
    async def test_headers_present(self, client):
        resp = await client.get("/test")
        assert resp.status_code == 200

        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["X-XSS-Protection"] == "1; mode=block"
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp
        assert "'unsafe-inline'" not in csp
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert resp.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_no_hsts_on_http(self, client):
        """HSTS should not be set for plain HTTP requests."""
        resp = await client.get("/test")
        assert "Strict-Transport-Security" not in resp.headers

    @pytest.mark.asyncio
    async def test_hsts_on_https(self):
        """HSTS should be set for HTTPS requests."""
        app = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as https_client:
            resp = await https_client.get("/test")
            assert "Strict-Transport-Security" in resp.headers
            assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]


class FakeRedis:
    def __init__(self, session_payload: str, csrf_token: str) -> None:
        self.session_payload = session_payload
        self.csrf_token = csrf_token

    async def get(self, key: str):
        if key.startswith(SESSION_REDIS_PREFIX):
            return self.session_payload
        if key.startswith(CSRF_REDIS_PREFIX):
            return self.csrf_token
        return None


class FakeAdminAuditPool:
    def __init__(self) -> None:
        self.executed: list[tuple[object, ...]] = []

    async def execute(self, _sql: str, *args):
        self.executed.append(args)
        return "INSERT 0 1"

    async def fetchrow(self, _sql: str, *_args):
        return {"enabled": True, "authorization_version": 1}


def _make_admin_rbac_app(roles: list[str]) -> tuple[FastAPI, str, str]:
    secret = "test-admin-secret"
    session_id = "session-1"
    csrf = "csrf-1"
    session_payload = (
        '{"sid":"session-1","admin_id":1,"username":"admin@example.com",'
        f'"roles":{roles!r},"authorization_version":1,'
        '"issued_at":1,"expires_at":4102444800}'
    ).replace("'", '"')

    app = FastAPI()
    app.state.redis = FakeRedis(session_payload, csrf)
    app.state.pg_pool = FakeAdminAuditPool()
    app.add_middleware(AdminAuthMiddleware, cookie_name="interlock_admin", secret_key=secret)

    @app.post("/api/data-sources")
    async def create_source():
        return {"ok": True}

    @app.post("/dashboard/source-wizard/save")
    async def save_source_wizard():
        return {"ok": True}

    @app.post("/dashboard/discovery/rescan")
    async def rescan_discovery():
        return {"ok": True}

    @app.post("/dashboard/connectors/probe")
    async def probe_connector():
        return {"ok": True}

    @app.post("/api/policies")
    async def create_policy():
        return {"ok": True}

    @app.post("/api/source-roles")
    async def create_source_role():
        return {"ok": True}

    @app.post("/api/identities/1/rotate")
    async def rotate_identity_key():
        return {"ok": True}

    @app.post("/api/approvals/1/reject")
    async def reject_approval():
        return {"ok": True}

    @app.get("/dashboard/audit-costs/export.csv")
    async def export_audit_csv():
        return {"ok": True}

    @app.get("/dashboard/access-control")
    async def access_control_page():
        return {"ok": True}

    @app.get("/dashboard/access-control/identities")
    async def identities_page():
        return {"ok": True}

    @app.get("/dashboard/policies")
    async def policies_page():
        return {"ok": True}

    @app.get("/dashboard/data-sources")
    async def data_sources_page():
        return {"ok": True}

    @app.get("/dashboard/write-safety")
    async def write_safety_page():
        return {"ok": True}

    @app.get("/openapi.json")
    async def openapi_json():
        return {"ok": True}

    @app.get("/dashboard/catalog")
    async def catalog_page():
        return {"ok": True}

    @app.post("/dashboard/data-sources/sales_pg/catalog/rescan")
    async def catalog_rescan():
        return {"ok": True}

    @app.post("/dashboard/data-sources/sales_pg/catalog/annotations")
    async def catalog_annotate():
        return {"ok": True}

    cookie = sign_cookie({"sid": session_id}, secret)
    return app, cookie, csrf


async def _post_as_roles(path: str, roles: list[str]):
    app, cookie, csrf = _make_admin_rbac_app(roles)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        client.cookies.set("interlock_admin", cookie)
        return await client.post(
            path,
            headers={"x-csrf-token": csrf},
        )


async def _get_as_roles(path: str, roles: list[str]):
    app, cookie, _csrf = _make_admin_rbac_app(roles)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        client.cookies.set("interlock_admin", cookie)
        return await client.get(path)


class TestAdminAuthRbac:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/api/data-sources",
            "/dashboard/source-wizard/save",
            "/dashboard/discovery/rescan",
            "/dashboard/connectors/probe",
            "/api/policies",
            "/api/identities/1/rotate",
            "/api/source-roles",
            "/api/admin-auth/oidc/admins/1",
            "/dashboard/data-sources/sales_pg/catalog/rescan",
            "/dashboard/data-sources/sales_pg/catalog/annotations",
        ],
    )
    async def test_admin_auditor_cannot_mutate_sources_policies_or_identities(
        self, path: str
    ) -> None:
        resp = await _post_as_roles(path, ["auditor"])
        assert resp.status_code == 403
        assert "role" in resp.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_approval_reviewer_can_reject_but_cannot_rotate_identity_key(self) -> None:
        approval = await _post_as_roles("/api/approvals/1/reject", ["approval_reviewer"])
        rotate = await _post_as_roles("/api/identities/1/rotate", ["approval_reviewer"])

        assert approval.status_code == 200
        assert rotate.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/api/data-sources",
            "/dashboard/source-wizard/save",
            "/dashboard/discovery/rescan",
            "/dashboard/connectors/probe",
            "/api/policies",
            "/api/identities/1/rotate",
            "/api/source-roles",
            "/api/approvals/1/reject",
        ],
    )
    async def test_owner_can_perform_all_admin_mutations(self, path: str) -> None:
        resp = await _post_as_roles(path, ["owner"])
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_source_admin_cannot_export_audit_csv(self) -> None:
        resp = await _get_as_roles("/dashboard/audit-costs/export.csv", ["source_admin"])
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_auditor_can_export_audit_csv(self) -> None:
        resp = await _get_as_roles("/dashboard/audit-costs/export.csv", ["auditor"])
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "allowed_role", "denied_role"),
        [
            ("/dashboard/access-control/identities", "security_admin", "auditor"),
            ("/dashboard/policies", "policy_admin", "auditor"),
            ("/dashboard/data-sources", "source_admin", "auditor"),
            ("/dashboard/write-safety", "approval_reviewer", "source_admin"),
            ("/openapi.json", "security_admin", "auditor"),
            ("/dashboard/catalog", "auditor", "approval_reviewer"),
            ("/dashboard/catalog", "policy_admin", "approval_reviewer"),
        ],
    )
    async def test_admin_read_rbac_matrix(
        self,
        path: str,
        allowed_role: str,
        denied_role: str,
    ) -> None:
        allowed = await _get_as_roles(path, [allowed_role])
        denied = await _get_as_roles(path, [denied_role])

        assert allowed.status_code == 200
        assert denied.status_code == 403

    @pytest.mark.asyncio
    async def test_openapi_docs_require_admin_auth(self) -> None:
        app, _cookie, _csrf = _make_admin_rbac_app(["security_admin"])
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/openapi.json")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_mutation_writes_control_plane_audit(self) -> None:
        app, cookie, csrf = _make_admin_rbac_app(["owner"])
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            client.cookies.set("interlock_admin", cookie)
            resp = await client.post("/api/policies", headers={"x-csrf-token": csrf})

        assert resp.status_code == 200
        audit_args = app.state.pg_pool.executed[-1]
        assert audit_args[2] == "POST /api/policies"
        assert audit_args[3] == "admin_route"
        assert audit_args[4] == "/api/policies"
        assert audit_args[8] is True

    @pytest.mark.asyncio
    async def test_admin_rbac_denial_writes_control_plane_audit(self) -> None:
        app, cookie, csrf = _make_admin_rbac_app(["auditor"])
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            client.cookies.set("interlock_admin", cookie)
            resp = await client.post("/api/policies", headers={"x-csrf-token": csrf})

        assert resp.status_code == 403
        audit_args = app.state.pg_pool.executed[-1]
        assert audit_args[2] == "POST /api/policies"
        assert audit_args[8] is False
        assert "does not allow" in audit_args[9]
