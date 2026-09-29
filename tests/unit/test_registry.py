"""Tests for MetadataRegistry."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.metadata.registry import MetadataRegistry
from interlock.models import DataSourceType


def _make_row(source_id: str = "src-1", name: str = "Test DB") -> dict:
    return {
        "source_id": source_id,
        "name": name,
        "source_type": "postgresql",
        "connection_config": {"host": "localhost", "port": 5432, "database": "test"},
        "cache_strategy": "deterministic_first",
        "enabled": True,
        "metadata": {"team": "eng"},
    }


@pytest.fixture
def mock_pool() -> AsyncMock:
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[_make_row()])
    pool.acquire = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_load_populates_sources(mock_pool: AsyncMock) -> None:
    reg = MetadataRegistry(mock_pool)
    await reg.load()

    assert len(reg.get_all()) == 1
    src = reg.get("src-1")
    assert src is not None
    assert src.name == "Test DB"
    assert src.source_type == DataSourceType.POSTGRESQL


@pytest.mark.asyncio
async def test_load_multiple_sources(mock_pool: AsyncMock) -> None:
    mock_pool.fetch.return_value = [
        _make_row("a", "Alpha"),
        _make_row("b", "Beta"),
    ]
    reg = MetadataRegistry(mock_pool)
    await reg.load()

    assert len(reg.get_all()) == 2
    assert reg.get("a") is not None
    assert reg.get("b") is not None


@pytest.mark.asyncio
async def test_load_defensively_skips_disabled_sources(mock_pool: AsyncMock) -> None:
    disabled = _make_row("disabled", "Disabled")
    disabled["enabled"] = False
    mock_pool.fetch.return_value = [_make_row("enabled", "Enabled"), disabled]

    reg = MetadataRegistry(mock_pool)
    await reg.load()

    assert reg.get("enabled") is not None
    assert reg.get("disabled") is None


@pytest.mark.asyncio
async def test_get_miss_returns_none(mock_pool: AsyncMock) -> None:
    reg = MetadataRegistry(mock_pool)
    await reg.load()

    assert reg.get("nonexistent") is None


@pytest.mark.asyncio
async def test_get_all_returns_copy(mock_pool: AsyncMock) -> None:
    reg = MetadataRegistry(mock_pool)
    await reg.load()

    all_sources = reg.get_all()
    all_sources["injected"] = MagicMock()
    # Original should be unaffected
    assert "injected" not in reg.get_all()


@pytest.mark.asyncio
async def test_setup_listener_subscribes(mock_pool: AsyncMock) -> None:
    conn = AsyncMock()
    mock_pool.acquire.return_value = conn

    reg = MetadataRegistry(mock_pool)
    await reg.setup_listener()

    conn.add_listener.assert_called_once_with("onyx_config_changed", reg._on_notify)


@pytest.mark.asyncio
async def test_on_notify_triggers_reload(mock_pool: AsyncMock) -> None:
    reg = MetadataRegistry(mock_pool)
    await reg.load()
    mock_pool.fetch.reset_mock()

    # Simulate a NOTIFY callback
    reg._on_notify(MagicMock(), 0, "onyx_config_changed", "")

    # Give the scheduled coroutine a chance to run
    await asyncio.sleep(0.05)

    mock_pool.fetch.assert_called_once()


@pytest.mark.asyncio
async def test_notify_during_reload_runs_second_reload_pass(mock_pool: AsyncMock) -> None:
    reg = MetadataRegistry(mock_pool)
    calls = 0

    async def slow_load() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            reg._on_notify(MagicMock(), 0, "onyx_config_changed", "")
            await asyncio.sleep(0)

    reg.load = AsyncMock(side_effect=slow_load)  # type: ignore[method-assign]

    reg._on_notify(MagicMock(), 0, "onyx_config_changed", "")
    assert reg._reload_task is not None
    await asyncio.wait_for(reg._reload_task, timeout=1)

    assert calls == 2
    assert reg._applied_reload_generation == reg._reload_generation == 2
