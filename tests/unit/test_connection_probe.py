"""Tests for ConnectionManager.probe_unsaved.

Covers the four shapes the source-onboarding UI relies on:
  - postgresql with missing host -> validation error
  - postgresql with unreachable host -> timeout / connection error
  - http with missing base_url -> validation error
  - http with reachable base_url -> healthy + latency_ms
  - http with unsafe private base_url -> validation error
  - unknown source_type -> typed error
"""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from interlock.connections.manager import ConnectionManager


@pytest.mark.asyncio
async def test_probe_pg_missing_host_returns_validation_error() -> None:
    result = await ConnectionManager.probe_unsaved("postgresql", {})
    assert result.healthy is False
    assert "host is required" in (result.error or "")


@pytest.mark.asyncio
async def test_probe_pg_unreachable_host_returns_error() -> None:
    result = await ConnectionManager.probe_unsaved(
        "postgresql",
        {"host": "127.0.0.1", "port": 1},
        timeout_seconds=1.0,
    )
    assert result.healthy is False
    assert result.error  # whatever it is, it's populated
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_probe_http_missing_base_url_returns_validation_error() -> None:
    result = await ConnectionManager.probe_unsaved("http", {})
    assert result.healthy is False
    assert "base_url is required" in (result.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_probe_http_reachable_base_url_returns_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "interlock.security.egress.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    respx.get("https://api.example.com").mock(return_value=Response(200, text="ok"))
    result = await ConnectionManager.probe_unsaved("http", {"base_url": "https://api.example.com"})
    assert result.healthy is True
    assert result.latency_ms >= 0
    assert result.error is None


@pytest.mark.asyncio
@respx.mock
async def test_probe_http_5xx_response_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "interlock.security.egress.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    respx.get("https://api.example.com").mock(return_value=Response(503))
    result = await ConnectionManager.probe_unsaved("http", {"base_url": "https://api.example.com"})
    assert result.healthy is False
    assert result.error and "503" in result.error


@pytest.mark.asyncio
async def test_probe_http_private_ip_blocked_by_default() -> None:
    result = await ConnectionManager.probe_unsaved(
        "http",
        {"base_url": "http://169.254.169.254/latest/meta-data"},
    )
    assert result.healthy is False
    assert "private" in (result.error or "").lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_value", ["false", "0", "no", "off", ""])
async def test_probe_legacy_false_strings_do_not_allow_private_egress(
    stored_value: str,
) -> None:
    result = await ConnectionManager.probe_unsaved(
        "http",
        {
            "base_url": "http://169.254.169.254/latest/meta-data",
            "allow_private_egress": stored_value,
        },
    )

    assert result.healthy is False
    assert "private" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_probe_enterprise_connector_private_base_url_blocked_before_adapter() -> None:
    result = await ConnectionManager.probe_unsaved(
        "search",
        {"base_url": "http://2130706433:9200"},
        connector_key="opensearch",
    )
    assert result.healthy is False
    assert "private" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_probe_unknown_source_type_returns_error() -> None:
    result = await ConnectionManager.probe_unsaved("mystery", {"foo": "bar"})
    assert result.healthy is False
    assert "probe not supported" in (result.error or "")


@pytest.mark.asyncio
async def test_probe_timeout_returns_typed_error() -> None:
    # Use a routable-but-blackhole port; asyncpg.connect will hang until
    # our wait_for fires. Setting a small timeout keeps the test fast.
    result = await ConnectionManager.probe_unsaved(
        "postgresql",
        {"host": "10.255.255.1", "port": 5432},
        timeout_seconds=0.5,
    )
    assert result.healthy is False
