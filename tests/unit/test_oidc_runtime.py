"""Runtime OIDC contract tests for Admin SSO and agent JWT identities."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from interlock.admin.app import create_app
from interlock.admin.auth import create_session, hash_password, sign_cookie
from interlock.admin.routes.auth import _consume_oidc_flow
from interlock.config import AuthConfig, InterLockConfig
from interlock.core.auth import AuthManager
from interlock.errors import IdentityNotFoundError
from interlock.models import OIDCTokens, OIDCUserInfo


def _jwt() -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps({"sub": "agent-sub"}).encode()).rstrip(b"=")
    return f"{header.decode()}.{body.decode()}.signature"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Redis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value.decode() if isinstance(value, bytes) else value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)
        return len(keys)

    async def getdel(self, key):
        return self.data.pop(key, None)

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key, "0")) + 1)
        return int(self.data[key])

    async def expire(self, key, seconds):
        return True

    async def ttl(self, key):
        return 300


class _LegacyRedis(_Redis):
    async def getdel(self, key):
        raise RuntimeError("unknown command GETDEL")

    async def eval(self, script, numkeys, key):
        assert numkeys == 1
        return self.data.pop(key, None)


class _AdminPool:
    def __init__(self, *, roles=None, oidc_subject="admin-sub") -> None:
        self.roles = roles or ["owner"]
        self.oidc_subject = oidc_subject
        self.enabled = True
        self.authorization_version = 1
        self.execute = AsyncMock(return_value="OK")

    async def fetchrow(self, query, *args):
        if "oidc_subject" in query and args and args[0] == self.oidc_subject:
            return {
                "id": 7,
                "username": "sso-admin",
                "roles": self.roles,
                "enabled": self.enabled,
                "authorization_version": self.authorization_version,
            }
        if "FROM admin_identities" in query and args and args[0] == "local-admin":
            return {
                "id": 8,
                "username": "local-admin",
                "password_hash": hash_password("recovery-password"),
                "roles": self.roles,
                "enabled": self.enabled,
                "authorization_version": self.authorization_version,
            }
        if "UPDATE admin_identities" in query and "oidc_subject" in query:
            return {
                "id": args[0],
                "name": "sso-admin",
                "oidc_subject": args[1],
                "email": args[2],
                "authorization_version": 2,
            }
        if "UPDATE identities" in query and "oidc_subject" in query:
            return {"id": args[0], "name": "agent", "oidc_subject": args[1]}
        if "authorization_version" in query and args and args[0] in {7, 8}:
            return {
                "enabled": self.enabled,
                "authorization_version": self.authorization_version,
            }
        return None


class _AdminOIDCProvider:
    def make_state(self):
        return "expected-state"

    def make_pkce_pair(self):
        return "pkce-verifier", "pkce-challenge"

    def get_authorization_url(self, state=None, *, nonce=None, code_challenge=None):
        return (
            f"https://idp.example/authorize?state={state}&nonce={nonce}"
            f"&code_challenge={code_challenge}&code_challenge_method=S256"
        )

    def verify_state(self, received, expected):
        return received == expected

    async def exchange_code(self, code, *, code_verifier):
        assert code_verifier == "pkce-verifier"
        return OIDCTokens(access_token="access", id_token="id-token")

    async def verify_token(self, token, *, nonce=None):
        assert nonce
        return OIDCUserInfo(sub="admin-sub", email="admin@example.com", groups=[])


def _admin_app(*, roles=None):
    config = InterLockConfig()
    config.admin.secret_key = "test-secret-key-not-for-production"
    config.auth.oidc.enabled = True
    config.auth.oidc.issuer_url = "https://idp.example"
    config.auth.oidc.admin_client_id = "admin-client"
    config.auth.oidc.admin_client_secret = "client-secret"
    app = create_app(config)
    app.state.pg_pool = _AdminPool(roles=roles)
    app.state.redis = _Redis()
    app.state.oidc_provider = _AdminOIDCProvider()
    app.state.admin_secret_key = config.admin.secret_key
    return app


def test_production_gateway_oidc_requires_agent_audience() -> None:
    with pytest.raises(ValidationError, match="agent_audience"):
        InterLockConfig.model_validate(
            {
                "environment": "production",
                "service_role": "gateway",
                "gateway": {"pg_trusted_tls_offload": True},
                "database": {"ssl_mode": "verify-full", "ssl_ca_file": "/ca.pem"},
                "audit": {"durability_mode": "strict"},
                "auth": {
                    "api_key_pepper": "p" * 32,
                    "allow_legacy_sha256_keys": False,
                    "oidc": {"enabled": True, "issuer_url": "https://idp.example"},
                },
            }
        )


def test_oidc_enabled_scopes_require_openid() -> None:
    with pytest.raises(ValidationError, match="must include openid"):
        AuthConfig.model_validate(
            {
                "oidc": {
                    "enabled": True,
                    "issuer_url": "https://idp.example",
                    "scopes": ["profile", "email"],
                }
            }
        )


def test_production_rejects_insecure_oidc_endpoint_escape_hatch() -> None:
    with pytest.raises(ValidationError, match="allow_insecure_endpoints"):
        InterLockConfig.model_validate(
            {
                "environment": "production",
                "service_role": "gateway",
                "gateway": {"pg_trusted_tls_offload": True},
                "database": {"ssl_mode": "verify-full", "ssl_ca_file": "/ca.pem"},
                "audit": {"durability_mode": "strict"},
                "auth": {
                    "api_key_pepper": "p" * 32,
                    "allow_legacy_sha256_keys": False,
                    "oidc": {
                        "enabled": True,
                        "issuer_url": "https://idp.example",
                        "agent_audience": "interlock-agent",
                        "allow_insecure_endpoints": True,
                    },
                },
            }
        )


@pytest.mark.anyio
async def test_admin_oidc_flow_is_one_time_and_creates_session() -> None:
    app = _admin_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.get("/auth/oidc/login", follow_redirects=False)
        assert started.status_code == 302
        query = parse_qs(urlsplit(started.headers["location"]).query)
        assert query["state"] == ["expected-state"]
        assert "nonce" in query
        assert query["code_challenge"] == ["pkce-challenge"]
        assert query["code_challenge_method"] == ["S256"]

        completed = await client.get(
            "/auth/oidc/callback",
            params={"code": "auth-code", "state": "expected-state"},
            follow_redirects=False,
        )

    assert completed.status_code == 302
    assert completed.headers["location"] == "/dashboard/overview"
    assert app.state.config.admin.cookie_name in completed.cookies
    assert not any(key.startswith("admin:oidc:flow:") for key in app.state.redis.data)


@pytest.mark.anyio
async def test_admin_oidc_flow_has_exactly_one_concurrent_callback_winner() -> None:
    app = _admin_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/auth/oidc/login", follow_redirects=False)

        async def callback():
            return await client.get(
                "/auth/oidc/callback",
                params={"code": "auth-code", "state": "expected-state"},
                follow_redirects=False,
            )

        first, second = await asyncio.gather(callback(), callback())

    locations = sorted([first.headers["location"], second.headers["location"]])
    assert locations == ["/auth/login?reason=oidc-error", "/dashboard/overview"]


@pytest.mark.anyio
async def test_oidc_flow_consumption_uses_atomic_lua_on_legacy_redis() -> None:
    redis = _LegacyRedis()
    redis.data["flow"] = "payload"
    assert await _consume_oidc_flow(redis, "flow") == "payload"
    assert await _consume_oidc_flow(redis, "flow") is None


@pytest.mark.anyio
async def test_security_admin_can_preprovision_oidc_subject_mappings() -> None:
    app = _admin_app()
    app.state.auth_disabled = True
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await client.put(
            "/api/admin-auth/oidc/admins/7",
            json={"subject": "issuer-admin-subject", "email": "admin@example.com"},
        )
        agent = await client.put(
            "/api/admin-auth/oidc/agents/31",
            json={"subject": "issuer-agent-subject"},
        )

    assert admin.status_code == 200
    assert admin.json()["subject"] == "issuer-admin-subject"
    assert admin.json()["authorization_version"] == 2
    assert agent.status_code == 200
    assert agent.json()["subject"] == "issuer-agent-subject"
    audit_actions = [
        call.args[3]
        for call in app.state.pg_pool.execute.await_args_list
        if len(call.args) > 3 and "admin_audit_log" in call.args[0]
    ]
    assert audit_actions == ["oidc.admin_mapping.set", "oidc.agent_mapping.set"]


@pytest.mark.anyio
async def test_oidc_subject_mapping_rejects_ambiguous_whitespace() -> None:
    app = _admin_app()
    app.state.auth_disabled = True
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/admin-auth/oidc/admins/7",
            json={"subject": " subject-with-space "},
        )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_admin_oidc_unknown_subject_cannot_auto_provision() -> None:
    app = _admin_app()
    app.state.pg_pool.oidc_subject = "different-subject"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/auth/oidc/login", follow_redirects=False)
        completed = await client.get(
            "/auth/oidc/callback",
            params={"code": "auth-code", "state": "expected-state"},
            follow_redirects=False,
        )
    assert completed.status_code == 302
    assert "reason=locked" in completed.headers["location"]


@pytest.mark.anyio
async def test_oidc_enabled_local_login_is_owner_break_glass_only() -> None:
    app = _admin_app(roles=["auditor"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            data={"username": "local-admin", "password": "recovery-password"},
            headers={"accept": "application/json"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "break_glass_owner_required"


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["disabled", "roles_changed"])
async def test_admin_session_is_revoked_after_authorization_change(mutation: str) -> None:
    app = _admin_app()
    sid, _ = await create_session(
        app.state.redis,
        admin_id=7,
        username="sso-admin",
        roles=["owner"],
        authorization_version=1,
        ttl_seconds=300,
    )
    cookie = sign_cookie({"sid": sid}, app.state.admin_secret_key)
    if mutation == "disabled":
        app.state.pg_pool.enabled = False
    else:
        app.state.pg_pool.authorization_version = 2

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(app.state.config.admin.cookie_name, cookie)
        response = await client.get("/openapi.json")

    assert response.status_code == 401
    assert not any(key.startswith("admin:session:") for key in app.state.redis.data)


@pytest.mark.anyio
async def test_agent_jwt_maps_to_preprovisioned_identity_and_database_grants() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": 31,
        "name": "oidc-agent",
        "agent_type": "custom",
        "team": "platform",
        "roles": ["global-observer"],
        "mapped_pg_role": "readonly",
        "metadata": {},
    }
    conn.fetch.return_value = [
        {
            "source_id": "source-a",
            "role_id": 91,
            "role_key": "reader",
            "role_name": "Reader",
            "review_required": False,
            "expires_at": None,
            "updated_at": "2026-08-13T00:00:00Z",
        }
    ]
    pool = MagicMock()
    pool.acquire.return_value = _Acquire(conn)
    pool.execute = AsyncMock(return_value="OK")
    redis = _Redis()
    provider = AsyncMock()
    provider.verify_token.return_value = OIDCUserInfo(sub="agent-sub")
    config = AuthConfig()
    config.oidc.enabled = True
    manager = AuthManager(pool, redis, config, oidc_provider=provider)

    identity = await manager.authenticate(_jwt())

    assert identity.identity_id == 31
    assert identity.auth_method == "oidc_jwt"
    assert identity.effective_source_role_keys("source-a") == {"reader"}
    provider.verify_token.assert_awaited_once()


@pytest.mark.anyio
async def test_agent_jwt_unknown_subject_is_rejected() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    pool = MagicMock()
    pool.acquire.return_value = _Acquire(conn)
    pool.execute = AsyncMock(return_value="OK")
    provider = AsyncMock()
    provider.verify_token.return_value = OIDCUserInfo(sub="unknown")
    config = AuthConfig()
    config.oidc.enabled = True
    manager = AuthManager(pool, _Redis(), config, oidc_provider=provider)

    with pytest.raises(IdentityNotFoundError, match="not provisioned"):
        await manager.authenticate(_jwt())
