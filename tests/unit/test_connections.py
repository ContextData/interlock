"""Tests for ConnectionManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interlock.connections.manager import ConnectionManager
from interlock.errors import DataSourceUnavailableError
from interlock.metadata.registry import MetadataRegistry
from interlock.models import DataSourceConfig, DataSourceType


def _make_config(source_id: str = "src-1") -> DataSourceConfig:
    return DataSourceConfig(
        source_id=source_id,
        name="Test DB",
        source_type=DataSourceType.POSTGRESQL,
        connection_config={
            "host": "db.example.com",
            "port": 5432,
            "database": "mydb",
            "allow_private_egress": True,
        },
    )


@pytest.fixture
def registry() -> MagicMock:
    reg = MagicMock(spec=MetadataRegistry)
    reg.get.return_value = _make_config()
    return reg


@pytest.mark.asyncio
@patch("interlock.connections.manager.asyncpg.create_pool", new_callable=AsyncMock)
async def test_get_pool_creates_lazily(mock_create_pool: AsyncMock, registry: MagicMock) -> None:
    fake_pool = AsyncMock()
    mock_create_pool.return_value = fake_pool

    mgr = ConnectionManager(registry)
    pool = await mgr.get_pool("src-1")

    assert pool is fake_pool
    mock_create_pool.assert_called_once()


@pytest.mark.asyncio
@patch("interlock.connections.manager.asyncpg.create_pool", new_callable=AsyncMock)
async def test_get_pool_reuses_existing(mock_create_pool: AsyncMock, registry: MagicMock) -> None:
    mock_create_pool.return_value = AsyncMock()

    mgr = ConnectionManager(registry)
    pool1 = await mgr.get_pool("src-1")
    pool2 = await mgr.get_pool("src-1")

    assert pool1 is pool2
    assert mock_create_pool.call_count == 1


@pytest.mark.asyncio
@patch("interlock.connections.manager.asyncpg.create_pool", new_callable=AsyncMock)
async def test_get_pool_with_role(mock_create_pool: AsyncMock, registry: MagicMock) -> None:
    mock_create_pool.return_value = AsyncMock()

    mgr = ConnectionManager(registry)
    await mgr.get_pool("src-1", pg_role="analyst")

    call_kwargs = mock_create_pool.call_args[1]
    assert call_kwargs["server_settings"] == {"role": "analyst"}


@pytest.mark.asyncio
async def test_get_pool_unknown_source(registry: MagicMock) -> None:
    registry.get.return_value = None
    mgr = ConnectionManager(registry)

    with pytest.raises(ValueError, match="Unknown data source"):
        await mgr.get_pool("no-such")


@pytest.mark.asyncio
async def test_get_pool_rejects_disabled_source(registry: MagicMock) -> None:
    config = _make_config()
    config.enabled = False
    registry.get.return_value = config
    mgr = ConnectionManager(registry)

    with pytest.raises(DataSourceUnavailableError, match="disabled"):
        await mgr.get_pool("src-1")


@pytest.mark.asyncio
@patch("interlock.connections.manager.asyncpg.create_pool", new_callable=AsyncMock)
async def test_health_check_healthy(mock_create_pool: AsyncMock, registry: MagicMock) -> None:
    fake_pool = AsyncMock()
    fake_pool.fetchval = AsyncMock(return_value=1)
    mock_create_pool.return_value = fake_pool

    mgr = ConnectionManager(registry)
    status = await mgr.health_check("src-1")

    assert status.healthy is True
    assert status.source_id == "src-1"
    assert status.latency_ms is not None
    assert status.error is None


@pytest.mark.asyncio
async def test_health_check_unhealthy(registry: MagicMock) -> None:
    registry.get.return_value = None
    mgr = ConnectionManager(registry)

    status = await mgr.health_check("bad-src")

    assert status.healthy is False
    assert status.error is not None


@pytest.mark.asyncio
@patch("interlock.connections.manager.asyncpg.create_pool", new_callable=AsyncMock)
async def test_close_all(mock_create_pool: AsyncMock, registry: MagicMock) -> None:
    fake_pool = AsyncMock()
    mock_create_pool.return_value = fake_pool

    mgr = ConnectionManager(registry)
    await mgr.get_pool("src-1")
    await mgr.close_all()

    fake_pool.close.assert_called_once()
    assert len(mgr._pools) == 0


@pytest.mark.asyncio
@patch("interlock.connections.manager.asyncpg.create_pool", new_callable=AsyncMock)
async def test_get_pool_with_connection_string(
    mock_create_pool: AsyncMock, registry: MagicMock
) -> None:
    config = DataSourceConfig(
        source_id="src-2",
        name="DSN source",
        source_type=DataSourceType.POSTGRESQL,
        connection_config={
            "connection_string": "postgres://user:pass@host/db",
            "allow_private_egress": True,
        },
    )
    registry.get.return_value = config
    mock_create_pool.return_value = AsyncMock()

    mgr = ConnectionManager(registry)
    await mgr.get_pool("src-2")

    call_kwargs = mock_create_pool.call_args[1]
    assert call_kwargs["dsn"] == "postgres://user:pass@host/db"
