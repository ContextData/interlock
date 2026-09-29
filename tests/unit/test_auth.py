"""Tests for interlock.core.auth - identity authentication and session management."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.admin.auth import hash_password
from interlock.config import AuthConfig
from interlock.core.auth import AuthManager
from interlock.errors import IdentityNotFoundError
from interlock.models import AgentType, IdentityContext
from interlock.security.api_keys import HMAC_SHA256, hash_api_key_for_storage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_identity_row(
    *,
    identity_id: int = 1,
    name: str = "test-agent",
    agent_type: str = "claude_code",
    team: str = "platform",
    roles: list[str] | None = None,
    mapped_pg_role: str = "readonly",
) -> dict:
    """Return a dict that behaves like an asyncpg.Record for our query columns."""
    return {
        "id": identity_id,
        "name": name,
        "agent_type": agent_type,
        "team": team,
        "roles": roles or ["read"],
        "mapped_pg_role": mapped_pg_role,
    }


class FakeRecord(dict):
    """Minimal asyncpg.Record stand-in that supports both [] and attribute access."""

    def __getitem__(self, key):
        return super().__getitem__(key)


def _record(data: dict) -> FakeRecord:
    return FakeRecord(data)


@pytest.fixture()
def config() -> AuthConfig:
    return AuthConfig(session_ttl_seconds=600)


@pytest.fixture()
def redis_client() -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock(return_value=True)
    client.delete = AsyncMock(return_value=1)
    return client


class _FakeAcquire:
    """Mimics the async context manager returned by asyncpg.Pool.acquire()."""

    def __init__(self, conn: AsyncMock) -> None:
        self.conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self.conn

    async def __aexit__(self, *args: object) -> None:
        pass


@pytest.fixture()
def pg_pool() -> AsyncMock:
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    pool.acquire.return_value = _FakeAcquire(conn)
    pool.execute = AsyncMock(return_value="UPDATE 1")
    # Stash conn for easy access in tests
    pool._conn = conn
    return pool


@pytest.fixture()
def auth(pg_pool, redis_client, config) -> AuthManager:
    return AuthManager(pg_pool, redis_client, config)


def _set_pg_row(pg_pool: MagicMock, row: dict | None) -> None:
    """Configure the mock PG pool to return a specific row."""
    pg_pool._conn.fetchrow.return_value = _record(row) if row else None


def _grant_row(
    *,
    source_id: str = "pg_reporting",
    role_id: int = 10,
    role_key: str = "analyst",
) -> FakeRecord:
    return _record(
        {
            "source_id": source_id,
            "role_id": role_id,
            "role_key": role_key,
            "role_name": role_key.title(),
            "review_required": False,
            "expires_at": None,
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHashApiKey:
    def test_deterministic(self) -> None:
        key = "onx_test_abc123"
        assert AuthManager.hash_api_key(key) == AuthManager.hash_api_key(key)

    def test_is_sha256_hex(self) -> None:
        key = "onx_test_abc123"
        expected = hashlib.sha256(key.encode()).hexdigest()
        assert AuthManager.hash_api_key(key) == expected

    def test_different_keys_different_hashes(self) -> None:
        assert AuthManager.hash_api_key("key-a") != AuthManager.hash_api_key("key-b")

    def test_production_pepper_uses_versioned_hmac(self) -> None:
        cfg = AuthConfig(api_key_pepper="p" * 32)
        digest, version = hash_api_key_for_storage("interlock-key-" + "x" * 32, cfg)
        assert version == HMAC_SHA256
        assert digest != AuthManager.hash_api_key("interlock-key-" + "x" * 32)


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_legacy_hash_is_upgraded_after_successful_auth(
        self, pg_pool, redis_client
    ) -> None:
        cfg = AuthConfig(session_ttl_seconds=600, api_key_pepper="p" * 32)
        manager = AuthManager(pg_pool, redis_client, cfg)
        raw_key = "interlock-key-" + "x" * 32
        row = _make_identity_row()
        row["api_key_hash"] = AuthManager.hash_api_key(raw_key)
        row["api_key_hash_version"] = "sha256-v1"
        _set_pg_row(pg_pool, row)

        await manager.authenticate(raw_key)

        upgrade_calls = [
            call
            for call in pg_pool.execute.await_args_list
            if "api_key_hash_version" in call.args[0]
        ]
        assert len(upgrade_calls) == 1
        assert upgrade_calls[0].args[2] == HMAC_SHA256

    @pytest.mark.asyncio
    async def test_valid_key_returns_identity(self, auth, pg_pool, redis_client) -> None:
        row = _make_identity_row()
        _set_pg_row(pg_pool, row)

        ctx = await auth.authenticate("my-api-key")

        assert isinstance(ctx, IdentityContext)
        assert ctx.identity_id == 1
        assert ctx.user == "test-agent"
        assert ctx.agent_type == AgentType.CLAUDE_CODE
        assert ctx.team == "platform"
        assert ctx.roles == ["read"]
        assert ctx.mapped_pg_role == "readonly"
        assert ctx.session_id is not None

    @pytest.mark.asyncio
    async def test_source_roles_hydrate_from_authoritative_grants(self, auth, pg_pool) -> None:
        row = _make_identity_row()
        row["metadata"] = {"source_roles": [{"source_id": "pg_reporting", "role": "stale"}]}
        _set_pg_row(pg_pool, row)
        pg_pool._conn.fetch.return_value = [_grant_row(role_key="analyst")]

        ctx = await auth.authenticate("my-api-key")

        assert ctx.source_roles == [
            {
                "source_id": "pg_reporting",
                "role_id": 10,
                "role": "analyst",
                "role_key": "analyst",
                "role_name": "Analyst",
                "review_required": False,
                "authority": "identity_source_role_grants",
            }
        ]
        assert ctx.effective_source_role_keys("pg_reporting") == {"analyst"}

    @pytest.mark.asyncio
    async def test_legacy_metadata_does_not_grant_when_grant_table_is_empty(
        self, auth, pg_pool
    ) -> None:
        row = _make_identity_row()
        row["metadata"] = {"source_roles": [{"source_id": "pg_reporting", "role": "stale"}]}
        _set_pg_row(pg_pool, row)
        pg_pool._conn.fetch.return_value = []

        ctx = await auth.authenticate("my-api-key")

        assert ctx.source_roles == []
        assert ctx.grants_version == "grants-empty"

    @pytest.mark.asyncio
    async def test_legacy_metadata_does_not_grant_when_grant_lookup_fails(
        self, auth, pg_pool
    ) -> None:
        row = _make_identity_row()
        row["metadata"] = {"source_roles": [{"source_id": "pg_reporting", "role": "stale"}]}
        _set_pg_row(pg_pool, row)
        pg_pool._conn.fetch.side_effect = RuntimeError("grant table unavailable")

        ctx = await auth.authenticate("my-api-key")

        assert ctx.source_roles == []
        assert ctx.effective_source_role_keys("pg_reporting") == set()
        assert ctx.grants_version == "grants-unavailable"

    @pytest.mark.asyncio
    async def test_caches_session_in_redis(self, auth, pg_pool, redis_client) -> None:
        _set_pg_row(pg_pool, _make_identity_row())

        ctx = await auth.authenticate("my-api-key")

        # Should have called redis.set at least once with the session key
        set_calls = redis_client.set.call_args_list
        assert len(set_calls) >= 1
        # One call should be for session:{session_id}
        session_keys = [str(c[0][0]) for c in set_calls]
        assert any(f"session:{ctx.session_id}" in k for k in session_keys)

    @pytest.mark.asyncio
    async def test_returns_cached_session_on_second_call(self, auth, pg_pool, redis_client) -> None:
        row = _make_identity_row()
        _set_pg_row(pg_pool, row)

        # First call - cache miss, goes to PG
        ctx1 = await auth.authenticate("my-api-key")

        # Configure Redis to return the cached context on next GET
        redis_client.get = AsyncMock(return_value=ctx1.model_dump_json().encode())

        # Second call - should hit cache, then revalidate identity/grants
        ctx2 = await auth.authenticate("my-api-key")

        assert ctx2.identity_id == ctx1.identity_id
        assert ctx2.user == ctx1.user

        assert pg_pool._conn.fetchrow.call_count == 2

    @pytest.mark.asyncio
    async def test_cached_identity_disabled_is_rejected_next_request(
        self, auth, pg_pool, redis_client
    ) -> None:
        ctx = IdentityContext(
            identity_id=42,
            user="cached-agent",
            agent_type=AgentType.CUSTOM,
            session_id="sess-disabled",
        )
        redis_client.get = AsyncMock(return_value=ctx.model_dump_json().encode())
        _set_pg_row(pg_pool, None)

        with pytest.raises(IdentityNotFoundError):
            await auth.authenticate("my-api-key")

        deleted_keys = [call.args[0] for call in redis_client.delete.call_args_list]
        assert f"session:{AuthManager.hash_api_key('my-api-key')}" in deleted_keys
        assert "session:sess-disabled" in deleted_keys

    @pytest.mark.asyncio
    async def test_revoked_source_grant_invalidates_cached_authorization(
        self, auth, pg_pool, redis_client
    ) -> None:
        cached = IdentityContext(
            identity_id=7,
            user="cached-agent",
            agent_type=AgentType.CUSTOM,
            team="platform",
            roles=["read"],
            source_roles=[
                {
                    "source_id": "pg_reporting",
                    "role_id": 10,
                    "role": "analyst",
                    "role_key": "analyst",
                    "role_name": "Analyst",
                    "review_required": False,
                    "authority": "identity_source_role_grants",
                }
            ],
            mapped_pg_role="readonly",
            session_id="sess-grants",
            grants_version="old-grants",
        )
        redis_client.get = AsyncMock(return_value=cached.model_dump_json().encode())
        _set_pg_row(pg_pool, _make_identity_row(identity_id=7, name="cached-agent"))
        pg_pool._conn.fetch.return_value = []

        ctx = await auth.authenticate("my-api-key")

        assert ctx.source_roles == []
        assert ctx.grants_version == "grants-empty"
        assert redis_client.set.call_count >= 2

    @pytest.mark.asyncio
    async def test_unknown_key_raises(self, auth, pg_pool) -> None:
        _set_pg_row(pg_pool, None)

        with pytest.raises(IdentityNotFoundError):
            await auth.authenticate("bad-key")

    @pytest.mark.asyncio
    async def test_disabled_identity_not_returned(self, auth, pg_pool) -> None:
        """The SQL query filters on enabled=TRUE, so a disabled identity
        results in no row returned and therefore IdentityNotFoundError."""
        _set_pg_row(pg_pool, None)  # PG returns nothing for disabled

        with pytest.raises(IdentityNotFoundError):
            await auth.authenticate("disabled-key")


class TestAuthenticatePostgres:
    @pytest.mark.asyncio
    async def test_pg_password_accepts_interlock_api_key(
        self,
        auth,
        pg_pool,
    ) -> None:
        _set_pg_row(pg_pool, _make_identity_row(name="api-agent"))

        ctx = await auth.authenticate_pg("any-pg-user", "interlock-api-key")

        assert ctx.user == "api-agent"
        assert ctx.session_id is not None

    @pytest.mark.asyncio
    async def test_pg_password_accepts_dedicated_identity_password(
        self,
        auth,
        pg_pool,
    ) -> None:
        row = _make_identity_row(name="pg-agent")
        row["pg_password_hash"] = hash_password("pg-secret")
        pg_pool._conn.fetchrow.side_effect = [None, _record(row)]

        ctx = await auth.authenticate_pg("pg-agent", "pg-secret")

        assert ctx.user == "pg-agent"
        assert ctx.session_id is not None
        assert pg_pool._conn.fetchrow.call_count == 2

    @pytest.mark.asyncio
    async def test_pg_password_rejects_bad_dedicated_password(
        self,
        auth,
        pg_pool,
    ) -> None:
        row = _make_identity_row(name="pg-agent")
        row["pg_password_hash"] = hash_password("pg-secret")
        pg_pool._conn.fetchrow.side_effect = [None, _record(row)]

        with pytest.raises(IdentityNotFoundError):
            await auth.authenticate_pg("pg-agent", "wrong")


class TestGetSession:
    @pytest.mark.asyncio
    async def test_returns_context_when_cached(self, auth, pg_pool, redis_client) -> None:
        ctx = IdentityContext(
            identity_id=5,
            user="cached-agent",
            agent_type=AgentType.CUSTOM,
            session_id="sess-abc",
        )
        redis_client.get = AsyncMock(return_value=ctx.model_dump_json().encode())
        _set_pg_row(
            pg_pool,
            _make_identity_row(identity_id=5, name="cached-agent", agent_type="custom"),
        )

        result = await auth.get_session("sess-abc")

        assert result is not None
        assert result.identity_id == 5
        assert result.session_id == "sess-abc"

    @pytest.mark.asyncio
    async def test_disabled_cached_session_returns_none_and_deletes_cache(
        self, auth, pg_pool, redis_client
    ) -> None:
        ctx = IdentityContext(
            identity_id=5,
            user="cached-agent",
            agent_type=AgentType.CUSTOM,
            session_id="sess-abc",
        )
        redis_client.get = AsyncMock(return_value=ctx.model_dump_json().encode())
        _set_pg_row(pg_pool, None)

        result = await auth.get_session("sess-abc")

        assert result is None
        redis_client.delete.assert_any_call("session:sess-abc")

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, auth, redis_client) -> None:
        redis_client.get = AsyncMock(return_value=None)

        result = await auth.get_session("nonexistent")
        assert result is None


class TestInvalidateSession:
    @pytest.mark.asyncio
    async def test_deletes_from_redis(self, auth, redis_client) -> None:
        await auth.invalidate_session("sess-xyz")

        redis_client.delete.assert_called_once_with("session:sess-xyz")

    @pytest.mark.asyncio
    async def test_redis_failure_does_not_raise(self, auth, redis_client) -> None:
        redis_client.delete = AsyncMock(side_effect=ConnectionError("down"))

        # Should not raise
        await auth.invalidate_session("sess-xyz")


class TestRedisFallthrough:
    @pytest.mark.asyncio
    async def test_pg_lookup_on_redis_failure(self, auth, pg_pool, redis_client) -> None:
        """When Redis GET fails, authenticate should fall through to PG."""
        redis_client.get = AsyncMock(side_effect=ConnectionError("Redis down"))
        # Redis SET will also fail, which is fine - best effort
        redis_client.set = AsyncMock(side_effect=ConnectionError("Redis down"))

        _set_pg_row(pg_pool, _make_identity_row())

        ctx = await auth.authenticate("my-api-key")

        assert ctx.identity_id == 1
        assert ctx.user == "test-agent"
        # PG was called despite Redis failure
        assert pg_pool._conn.fetchrow.call_count == 1
