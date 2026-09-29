"""Unit tests for interlock.db connection utilities."""

from __future__ import annotations

import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interlock.config import DatabaseConfig, RedisConfig
from interlock.errors import ConfigValidationError

# ---------------------------------------------------------------------------
# Pool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pg_pool_passes_config_params():
    """create_pg_pool should forward DatabaseConfig fields to asyncpg.create_pool."""
    cfg = DatabaseConfig(
        host="db.example.com",
        port=5433,
        database="testdb",
        user="tester",
        password="secret",
        min_pool=3,
        max_pool=15,
    )

    sentinel_pool = MagicMock(name="pool")
    with patch(
        "interlock.db.pool.asyncpg.create_pool", new_callable=AsyncMock, return_value=sentinel_pool
    ) as mock_create:
        from interlock.db.pool import create_pg_pool

        result = await create_pg_pool(cfg)

    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["host"] == "db.example.com"
    assert call_kwargs["port"] == 5433
    assert call_kwargs["database"] == "testdb"
    assert call_kwargs["user"] == "tester"
    assert call_kwargs["password"] == "secret"
    assert call_kwargs["min_size"] == 3
    assert call_kwargs["max_size"] == 15
    assert call_kwargs["statement_cache_size"] == 0
    assert call_kwargs["ssl"] is None
    assert call_kwargs["init"] is not None  # JSON codec initializer
    assert result is sentinel_pool


@pytest.mark.asyncio
async def test_create_pg_pool_uses_verified_tls_context() -> None:
    cfg = DatabaseConfig(
        ssl_mode="verify-full",
        ssl_ca_file="/run/secrets/control-db-ca.pem",
    )
    sentinel_context = MagicMock()
    sentinel_pool = MagicMock(name="pool")
    with (
        patch(
            "interlock.db.pool.ssl.create_default_context", return_value=sentinel_context
        ) as create_ssl,
        patch(
            "interlock.db.pool.asyncpg.create_pool",
            new_callable=AsyncMock,
            return_value=sentinel_pool,
        ) as create_pool,
    ):
        from interlock.db.pool import create_pg_pool

        await create_pg_pool(cfg)

    create_ssl.assert_called_once_with(cafile="/run/secrets/control-db-ca.pem")
    assert sentinel_context.verify_mode != 0
    assert sentinel_context.check_hostname is True
    assert create_pool.call_args.kwargs["ssl"] is sentinel_context


@pytest.mark.asyncio
async def test_create_pg_pool_require_uses_encryption_without_ca_verification() -> None:
    cfg = DatabaseConfig(ssl_mode="require")
    sentinel_pool = MagicMock(name="pool")
    with patch(
        "interlock.db.pool.asyncpg.create_pool",
        new_callable=AsyncMock,
        return_value=sentinel_pool,
    ) as create_pool:
        from interlock.db.pool import create_pg_pool

        await create_pg_pool(cfg)

    context = create_pool.call_args.kwargs["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


@pytest.mark.asyncio
async def test_create_pg_pool_rejects_client_cert_without_tls() -> None:
    cfg = DatabaseConfig(
        ssl_mode="disable",
        ssl_cert_file="/run/secrets/control-db-client.pem",
        ssl_key_file="/run/secrets/control-db-client.key",
    )
    from interlock.db.pool import create_pg_pool

    with pytest.raises(ConfigValidationError, match="ssl_mode"):
        await create_pg_pool(cfg)


@pytest.mark.asyncio
async def test_create_pg_pool_rejects_partial_client_cert_pair() -> None:
    cfg = DatabaseConfig(
        ssl_mode="require",
        ssl_cert_file="/run/secrets/control-db-client.pem",
    )
    from interlock.db.pool import create_pg_pool

    with pytest.raises(ConfigValidationError, match="configured together"):
        await create_pg_pool(cfg)


@pytest.mark.asyncio
async def test_close_pg_pool():
    """close_pg_pool should call pool.close()."""
    mock_pool = AsyncMock()
    from interlock.db.pool import close_pg_pool

    await close_pg_pool(mock_pool)
    mock_pool.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Redis tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_redis_client_passes_config_params():
    """create_redis_client should build a ConnectionPool from config.url."""
    cfg = RedisConfig(url="redis://redis.local:6380/2", max_connections=50)

    mock_pool_instance = MagicMock(name="connection_pool")
    mock_client = MagicMock(name="redis_client")

    with (
        patch(
            "interlock.db.redis.aioredis.ConnectionPool.from_url", return_value=mock_pool_instance
        ) as mock_from_url,
        patch("interlock.db.redis.aioredis.Redis", return_value=mock_client) as mock_redis_cls,
    ):
        from interlock.db.redis import create_redis_client

        result = await create_redis_client(cfg)

    mock_from_url.assert_called_once_with(
        "redis://redis.local:6380/2",
        max_connections=50,
        decode_responses=False,
    )
    mock_redis_cls.assert_called_once_with(connection_pool=mock_pool_instance)
    assert result is mock_client


@pytest.mark.asyncio
async def test_close_redis_client():
    """close_redis_client should call client.aclose()."""
    mock_client = AsyncMock()
    from interlock.db.redis import close_redis_client

    await close_redis_client(mock_client)
    mock_client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Notify tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_pg_listeners_subscribes_channels():
    """setup_pg_listeners should acquire a connection and add listeners."""
    mock_conn = AsyncMock()
    mock_pool = AsyncMock()
    mock_pool.acquire.return_value = mock_conn

    cb1 = AsyncMock(name="cb1")
    cb2 = AsyncMock(name="cb2")
    channels = {"onyx_config_changed": cb1, "onyx_policy_changed": cb2}

    from interlock.db.notify import setup_pg_listeners

    conn = await setup_pg_listeners(mock_pool, channels)

    assert conn is mock_conn
    mock_pool.acquire.assert_awaited_once()
    assert mock_conn.add_listener.await_count == 2
    mock_conn.add_listener.assert_any_await("onyx_config_changed", cb1)
    mock_conn.add_listener.assert_any_await("onyx_policy_changed", cb2)


def test_default_channels_exported():
    """DEFAULT_CHANNELS should be importable from interlock.db."""
    from interlock.db import DEFAULT_CHANNELS

    assert "onyx_config_changed" in DEFAULT_CHANNELS
    assert "onyx_policy_changed" in DEFAULT_CHANNELS
    assert "onyx_write_approval" in DEFAULT_CHANNELS
