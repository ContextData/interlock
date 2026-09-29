"""Tests for auth/policy/rate-limiter wiring across admin, MCP, and HTTP proxy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from interlock.config import InterLockConfig
from interlock.errors import IdentityNotFoundError
from interlock.gateway.http_proxy import HTTPProxy
from interlock.gateway.mcp_adapter import MCPAdapter
from interlock.models import (
    AgentType,
    IdentityContext,
    PolicyDecision,
    RateLimitResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity() -> IdentityContext:
    return IdentityContext(
        identity_id=1,
        user="test-user",
        agent_type=AgentType.CUSTOM,
        team="eng",
        roles=["analyst"],
        session_id="sess-1",
    )


def _make_auth_manager(identity: IdentityContext | None = None, fail: bool = False):
    mgr = MagicMock()
    if fail:
        mgr.authenticate = AsyncMock(side_effect=IdentityNotFoundError("bad key"))
    else:
        mgr.authenticate = AsyncMock(return_value=identity or _identity())
    return mgr


def _make_policy_engine(allowed: bool = True):
    engine = MagicMock()
    engine.evaluate = MagicMock(
        return_value=PolicyDecision(
            allowed=allowed,
            reason="test policy" if allowed else "denied by test",
        )
    )
    return engine


# ---------------------------------------------------------------------------
# Task 1: Admin security headers
# ---------------------------------------------------------------------------


class TestAdminSecurityHeaders:
    """Verify that SecurityHeadersMiddleware is wired into the admin app."""

    def test_health_has_security_headers(self):
        with (
            patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock),
            patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
            patch("interlock.admin.app.create_redis_client", new_callable=AsyncMock),
            patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
        ):
            from interlock.admin.app import create_app

            app = create_app(InterLockConfig())
            client = TestClient(app)
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.headers.get("x-content-type-options") == "nosniff"
            assert resp.headers.get("x-frame-options") == "DENY"
            assert resp.headers.get("x-xss-protection") == "1; mode=block"
            assert resp.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Task 3a: MCP adapter auth wiring
# ---------------------------------------------------------------------------


def _mcp_app(
    auth_manager=None,
    policy_engine=None,
    pg_pool=None,
) -> Starlette:
    adapter = MCPAdapter(pg_pool=pg_pool)
    app = Starlette(routes=adapter.get_routes())
    if auth_manager is not None:
        app.state.auth_manager = auth_manager
    if policy_engine is not None:
        app.state.policy_engine = policy_engine
    return app


class TestMCPAuthWiring:
    """MCP adapter auth and policy tests."""

    def test_no_auth_configured_passes_through(self):
        """When no auth_manager on app.state, requests pass without auth."""
        pool = MagicMock()
        pool.fetch = AsyncMock(return_value=[])
        app = _mcp_app(pg_pool=pool)
        client = TestClient(app)
        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "s1", "sql": "SELECT 1"}},
        )
        # Should not be 401/403 - auth is not configured
        assert resp.status_code != 401
        assert resp.status_code != 403

    def test_valid_bearer_token_authenticates(self):
        """With auth_manager, a valid Bearer token authenticates successfully."""
        pool = MagicMock()
        pool.fetch = AsyncMock(return_value=[])
        auth = _make_auth_manager()
        app = _mcp_app(auth_manager=auth, pg_pool=pool)
        client = TestClient(app)
        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "s1", "sql": "SELECT 1"}},
            headers={"Authorization": "Bearer valid-key"},
        )
        assert resp.status_code != 401
        auth.authenticate.assert_called_once_with("valid-key")

    def test_invalid_bearer_token_returns_401(self):
        """With auth_manager, an invalid Bearer token returns 401."""
        auth = _make_auth_manager(fail=True)
        app = _mcp_app(auth_manager=auth)
        client = TestClient(app)
        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "s1", "sql": "SELECT 1"}},
            headers={"Authorization": "Bearer bad-key"},
        )
        assert resp.status_code == 401

    def test_missing_bearer_returns_401_when_auth_configured(self):
        """With auth_manager, missing Authorization header returns 401."""
        auth = _make_auth_manager()
        app = _mcp_app(auth_manager=auth)
        client = TestClient(app)
        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "s1", "sql": "SELECT 1"}},
        )
        assert resp.status_code == 401

    def test_policy_deny_returns_403(self):
        """With auth and policy, a denied policy returns 403."""
        pool = MagicMock()
        pool.fetch = AsyncMock(return_value=[])
        auth = _make_auth_manager()
        policy = _make_policy_engine(allowed=False)
        app = _mcp_app(auth_manager=auth, policy_engine=policy, pg_pool=pool)
        client = TestClient(app)
        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "s1", "sql": "SELECT 1"}},
            headers={"Authorization": "Bearer valid-key"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Task 3b: HTTP proxy auth wiring
# ---------------------------------------------------------------------------


def _http_proxy_app(
    auth_manager=None,
    policy_engine=None,
    registry=None,
) -> Starlette:
    proxy = HTTPProxy()
    app = Starlette(routes=proxy.get_routes())
    if auth_manager is not None:
        app.state.auth_manager = auth_manager
    if policy_engine is not None:
        app.state.policy_engine = policy_engine
    if registry is not None:
        app.state.registry = registry
    return app


class TestHTTPProxyAuthWiring:
    """HTTP proxy auth and policy tests."""

    def test_no_auth_configured_passes_through(self):
        """When no auth_manager on app.state, requests pass without auth."""
        registry = MagicMock()
        registry.get.return_value = None
        app = _http_proxy_app(registry=registry)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/proxy/src1/some/path")
        # Should not be 401 - auth not configured. May be 404/503 due to missing source.
        assert resp.status_code != 401

    def test_missing_bearer_returns_401_when_auth_configured(self):
        """With auth_manager, missing Authorization header returns 401."""
        auth = _make_auth_manager()
        app = _http_proxy_app(auth_manager=auth)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/proxy/src1/some/path")
        assert resp.status_code == 401

    def test_invalid_bearer_returns_401(self):
        """With auth_manager, an invalid Bearer token returns 401."""
        auth = _make_auth_manager(fail=True)
        app = _http_proxy_app(auth_manager=auth)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/proxy/src1/some/path",
            headers={"Authorization": "Bearer bad-key"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Task 2: Rate limiter blocks after exceeding limit (mock)
# ---------------------------------------------------------------------------


class TestRateLimiterWiring:
    """Verify rate limiter is checked in PG proxy _handle_simple_query."""

    @pytest.mark.asyncio
    async def test_rate_limiter_blocks_when_exceeded(self):
        """Rate limiter returning allowed=False causes an error response."""

        from interlock.gateway.pg_proxy import PGProxy

        rate_limiter = MagicMock()
        rate_limiter.check = AsyncMock(
            return_value=RateLimitResult(
                allowed=False,
                remaining=0,
                limit=100,
                reset_at=0.0,
            )
        )

        proxy = PGProxy(
            listen_host="127.0.0.1",
            listen_port=15432,
            upstream_host="127.0.0.1",
            upstream_port=15433,
            rate_limiter=rate_limiter,
        )

        identity = _identity()

        # Mock writers
        client_writer = MagicMock()
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()

        upstream_reader = AsyncMock()
        upstream_writer = MagicMock()
        upstream_writer.write = MagicMock()
        upstream_writer.drain = AsyncMock()

        # Build a simple query payload (null-terminated SQL)
        sql = b"SELECT 1\x00"

        await proxy._handle_simple_query(
            sql,
            client_writer,
            upstream_reader,
            upstream_writer,
            identity=identity,
        )

        # Should have written an error to the client
        client_writer.write.assert_called_once()
        written_bytes = client_writer.write.call_args[0][0]
        assert b"Rate limit exceeded" in written_bytes

        # Should NOT have forwarded anything to upstream
        upstream_writer.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limiter_allows_when_within_limit(self):
        """Rate limiter returning allowed=True lets the query proceed."""

        from interlock.gateway.pg_proxy import PGProxy

        rate_limiter = MagicMock()
        rate_limiter.check = AsyncMock(
            return_value=RateLimitResult(
                allowed=True,
                remaining=99,
                limit=100,
                reset_at=0.0,
            )
        )

        proxy = PGProxy(
            listen_host="127.0.0.1",
            listen_port=15432,
            upstream_host="127.0.0.1",
            upstream_port=15433,
            rate_limiter=rate_limiter,
        )

        identity = _identity()

        client_writer = MagicMock()
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()

        # Upstream reader will return a ReadyForQuery message
        rfq_payload = b"I"
        from interlock.gateway.pg_messages import MSG_TYPE_READY_FOR_QUERY as RFQ

        async def fake_read_message(reader):
            return (RFQ, rfq_payload)

        upstream_reader = MagicMock()
        upstream_writer = MagicMock()
        upstream_writer.write = MagicMock()
        upstream_writer.drain = AsyncMock()

        sql = b"SELECT 1\x00"

        with patch(
            "interlock.gateway.pg_proxy.read_message",
            new=AsyncMock(return_value=(RFQ, rfq_payload)),
        ):
            await proxy._handle_simple_query(
                sql,
                client_writer,
                upstream_reader,
                upstream_writer,
                identity=identity,
            )

        # The query should have been forwarded to upstream
        upstream_writer.write.assert_called()
