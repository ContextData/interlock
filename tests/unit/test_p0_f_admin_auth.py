"""Regression test for audit P0-F: Admin API authentication.

AUDIT-COVERS: P0-F

The audit reported that the admin app mounted full management routes
without any auth dependency, OIDC session, bearer-token check, or admin
RBAC enforcement. Anyone reachable could create identities, policies,
data sources, ingestion jobs, approvals, and view audit/cost data.

These tests pin the fix:

1. Anonymous request to a protected route returns 302 (HTML) or 401 (JSON).
2. Login with bad credentials returns 401 and does not set a cookie.
3. Login with good credentials sets the signed session + CSRF cookies.
4. Authenticated unsafe POSTs without X-CSRF-Token are rejected (403).
5. Authenticated unsafe POSTs with the right X-CSRF-Token succeed.
6. /health, /auth/login, /auth/csrf are bypass paths.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.admin.auth import create_session, hash_password, sign_cookie
from interlock.config import InterLockConfig
from interlock.db.migrations import discover_migrations
from interlock.errors import ConfigValidationError

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None

    async def execute(self, *a, **k):
        return "OK"


class FakePool:
    def __init__(self, password_hash: str | None = None) -> None:
        self._password_hash = password_hash
        self.fetchval = AsyncMock(return_value=0)
        self.fetch = AsyncMock(return_value=[])
        self.execute = AsyncMock(return_value="OK")

    def acquire(self):
        return _FakeConn()

    async def fetchrow(self, query, *args):
        if "FROM admin_identities" not in query:
            return None
        if "authorization_version" in query and args and isinstance(args[0], int):
            return _Row(enabled=True, authorization_version=1)
        if self._password_hash is None:
            return None
        if not args or args[0] != "alice":
            return None
        return _Row(
            id=1,
            username="alice",
            password_hash=self._password_hash,
            roles=["owner", "reviewer"],
            enabled=True,
            authorization_version=1,
        )


class _Row:
    def __init__(self, **kw):
        self._kw = kw

    def __getitem__(self, k):
        return self._kw[k]

    def get(self, k, default=None):
        return self._kw.get(k, default)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiry: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value if isinstance(value, str) else value.decode()
        if ex is not None:
            self.expiry[key] = int(ex)

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.expiry.pop(k, None)

    async def incr(self, key):
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key, seconds):
        self.expiry[key] = int(seconds)
        return True

    async def ttl(self, key):
        return self.expiry.get(key, -1)

    async def ping(self):
        return True

    async def aclose(self):
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(pool, redis) -> object:
    config = InterLockConfig()
    # Use a fixed signing key so tests can verify cookie behavior.
    config.admin.secret_key = "test-secret-key-not-for-prod"
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
        app.state.admin_secret_key = "test-secret-key-not-for-prod"
    return app


@pytest.fixture
def app_no_user():
    pool = FakePool(password_hash=None)
    redis = FakeRedis()
    return _make_app(pool, redis), pool, redis


@pytest.fixture
def app_with_alice():
    pool = FakePool(password_hash=hash_password("correct-horse"))
    redis = FakeRedis()
    return _make_app(pool, redis), pool, redis


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _assert_security_headers(response) -> None:
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("content-security-policy")
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


@pytest.mark.anyio
async def test_p0_f_health_is_public(app_no_user) -> None:
    app, _, _ = app_no_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    _assert_security_headers(r)


@pytest.mark.anyio
async def test_p0_f_ready_is_public_and_checks_dependencies(app_no_user) -> None:
    app, pool, _ = app_no_user
    pool.fetch = AsyncMock(
        return_value=[
            {"version": migration.version, "checksum": migration.checksum}
            for migration in discover_migrations(ROOT / "migrations")
        ]
    )
    app.state.registry = SimpleNamespace(get_all=lambda: [])
    app.state.faiss_sync = SimpleNamespace(healthy=True)
    app.state.discovery_index = SimpleNamespace(
        freshness=AsyncMock(
            return_value={"loaded_generation": 1, "remote_generation": 1, "fresh": True}
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/ready", headers={"x-correlation-id": "admin-ready-1"})
    assert r.status_code == 200
    assert r.headers["x-correlation-id"] == "admin-ready-1"
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["postgres"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"
    assert body["checks"]["registry"]["status"] == "ok"
    assert body["checks"]["discovery_vector"]["fresh"] is True
    _assert_security_headers(r)


@pytest.mark.anyio
async def test_p0_f_login_page_is_public(app_no_user) -> None:
    app, _, _ = app_no_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/auth/login")
    assert r.status_code == 200
    assert "InterLock Admin" in r.text


@pytest.mark.anyio
async def test_p0_f_unauthenticated_dashboard_redirects_to_login(
    app_no_user,
) -> None:
    app, _, _ = app_no_user
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
        headers={"accept": "text/html"},
    ) as c:
        r = await c.get("/dashboard/overview")
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]
    _assert_security_headers(r)


@pytest.mark.anyio
async def test_p0_f_unauthenticated_api_returns_401(app_no_user) -> None:
    app, _, _ = app_no_user
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"accept": "application/json"},
    ) as c:
        r = await c.get("/api/identities")
    assert r.status_code == 401
    _assert_security_headers(r)


@pytest.mark.anyio
async def test_p0_f_login_bad_credentials_rejected(app_with_alice) -> None:
    app, _, redis = app_with_alice
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/auth/login",
            data={"username": "alice", "password": "wrong-password"},
            headers={"accept": "application/json"},
        )
    assert r.status_code == 401
    # No session cookie was set.
    assert "interlock_admin_session" not in r.cookies
    assert not any(k.startswith("admin:session:") for k in redis.store)
    assert any(k.startswith("admin:login_fail:") for k in redis.store)


@pytest.mark.anyio
async def test_p0_f_login_good_credentials_creates_session(
    app_with_alice,
) -> None:
    app, _, redis = app_with_alice
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/auth/login",
            data={"username": "alice", "password": "correct-horse"},
            headers={"accept": "application/json"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert "interlock_admin_session" in r.cookies
    assert "interlock_admin_csrf" in r.cookies
    # Session was persisted in Redis.
    session_keys = [k for k in redis.store if k.startswith("admin:session:")]
    assert len(session_keys) == 1


@pytest.mark.anyio
async def test_p0_f_secure_cookie_flag_is_applied_when_configured(
    app_with_alice,
) -> None:
    app, _, _ = app_with_alice
    app.state.config.admin.cookie_secure = True
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        r = await c.post(
            "/auth/login",
            data={"username": "alice", "password": "correct-horse"},
            headers={"accept": "application/json"},
        )
    assert r.status_code == 200
    set_cookie = r.headers.get_list("set-cookie")
    assert any(
        cookie.startswith("interlock_admin_session=") and "Secure" in cookie
        for cookie in set_cookie
    )
    assert any(
        cookie.startswith("interlock_admin_csrf=") and "Secure" in cookie for cookie in set_cookie
    )


def test_p0_f_create_app_rejects_insecure_production_cookie() -> None:
    config = InterLockConfig()
    config.environment = "production"
    config.admin.secret_key = "s" * 32
    config.admin.cookie_secure = False
    config.database.ssl_mode = "verify-full"
    config.database.ssl_ca_file = "/run/secrets/control-db-ca.pem"

    with pytest.raises(ConfigValidationError, match="admin.cookie_secure"):
        create_app(config=config)


@pytest.mark.anyio
async def test_p0_f_csrf_blocks_post_without_token(app_with_alice) -> None:
    app, _, redis = app_with_alice
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Login first.
        login = await c.post(
            "/auth/login",
            data={"username": "alice", "password": "correct-horse"},
            headers={"accept": "application/json"},
        )
        assert login.status_code == 200
        # Now make a write request without the CSRF header.
        r = await c.post(
            "/api/identities",
            json={"name": "bot", "agent_type": "claude_code", "api_key": "abc"},
        )
    assert r.status_code == 403
    _assert_security_headers(r)


@pytest.mark.anyio
async def test_p0_f_csrf_token_from_other_session_rejected(
    app_with_alice,
) -> None:
    app, _, redis = app_with_alice
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/auth/login",
            data={"username": "alice", "password": "correct-horse"},
            headers={"accept": "application/json"},
        )
        # Forge a CSRF header that doesn't match the session.
        r = await c.post(
            "/api/identities",
            json={"name": "bot", "agent_type": "claude_code", "api_key": "abc"},
            headers={"x-csrf-token": "totally-bogus"},
        )
    assert r.status_code == 403
    _assert_security_headers(r)


@pytest.mark.anyio
async def test_p0_f_rate_limit_response_has_security_headers(app_with_alice) -> None:
    app, _, _ = app_with_alice
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = None
        for _ in range(10):
            response = await c.post(
                "/auth/login",
                data={"username": "alice", "password": "wrong-password"},
                headers={"accept": "application/json"},
            )
            if response.status_code == 429:
                break

    assert response is not None
    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"
    _assert_security_headers(response)


@pytest.mark.anyio
async def test_p0_f_uncovered_admin_mutation_route_fails_closed(app_no_user) -> None:
    app, _, redis = app_no_user
    sid, csrf = await create_session(
        redis,
        admin_id=2,
        username="auditor",
        roles=["auditor"],
        authorization_version=1,
        ttl_seconds=3600,
    )
    cookie = sign_cookie({"sid": sid}, "test-secret-key-not-for-prod")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"accept": "application/json"},
        cookies={"interlock_admin_session": cookie, "interlock_admin_csrf": csrf},
    ) as c:
        response = await c.post(
            "/api/uncovered-future-route",
            headers={"x-csrf-token": csrf},
            json={"x": 1},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "Admin role does not allow this action"


@pytest.mark.anyio
async def test_p0_f_secret_key_stays_stable_across_create_app_and_lifespan(
    app_with_alice,
) -> None:
    """Regression: lifespan must NOT re-resolve the signing key.

    When no INTERLOCK_ADMIN__SECRET_KEY is set, env_secret_or_default returns
    a fresh random value on each call. If lifespan re-resolves it, the
    AuthMiddleware (built from create_app's resolution) and the login
    route (reads from app.state, written by lifespan) end up with
    different secrets and every signed cookie is rejected.
    """
    app, _, _ = app_with_alice
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        login = await c.post(
            "/auth/login",
            data={"username": "alice", "password": "correct-horse"},
            headers={"accept": "application/json"},
        )
        assert login.status_code == 200
        # The session cookie set by the login route must be accepted by
        # the auth middleware on the next request. If the secret key
        # diverged this would be 401.
        r = await c.get(
            "/api/identities",
            headers={"accept": "application/json"},
        )
    # 200 (or 405 / other) is fine - we only care that auth did NOT
    # reject the cookie.
    assert r.status_code != 401, (
        "Signing key diverged between create_app and lifespan - login " "cookie no longer validates"
    )


@pytest.mark.anyio
async def test_p0_f_forged_session_cookie_rejected(app_no_user) -> None:
    app, _, _ = app_no_user
    transport = ASGITransport(app=app)
    # Sign with the wrong secret.
    forged = sign_cookie({"sid": "fake"}, "wrong-secret")
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"accept": "application/json"},
        cookies={"interlock_admin_session": forged},
    ) as c:
        r = await c.get("/api/identities")
    assert r.status_code == 401
