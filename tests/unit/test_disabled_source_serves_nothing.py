"""A disabled or deleted source must stop answering at once, cache included.

The registry loads only enabled sources, so a disabled source reads as
unknown. The MCP handler used to consult the cache before it ever asked the
registry, so a statement cached while the source was enabled kept being
served after it was disabled.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.cache.generation import SourceGenerationBarrier
from interlock.config import InterLockConfig
from interlock.gateway.mcp_adapter import MCPAdapter
from interlock.gateway.pg_messages import MSG_TYPE_QUERY, MSG_TYPE_TERMINATE, pack_message
from interlock.gateway.pg_proxy import PGProxy
from interlock.metadata.registry import MetadataRegistry
from interlock.models import DataSourceConfig, DataSourceType

ROOT = Path(__file__).resolve().parents[2]


def _cache_hit() -> AsyncMock:
    strategy = AsyncMock()
    result = MagicMock(hit=True, data=b'[{"secret": 1}]', tier="l1")
    strategy.get.return_value = result
    return strategy


def _request(state: SimpleNamespace) -> MagicMock:
    request = MagicMock()
    request.app.state = state
    request.state = SimpleNamespace(gateway_decision=None, identity=None)
    return request


def _state(registry: Any, strategy: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        cache_strategy=strategy,
        registry=registry,
        audit_logger=None,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )


@pytest.mark.asyncio
async def test_mcp_refuses_a_cached_statement_once_the_source_is_gone() -> None:
    registry = MagicMock()
    registry.get.return_value = None
    strategy = _cache_hit()
    adapter = MCPAdapter(pg_pool=AsyncMock())

    resp = await adapter._execute_query(
        _request(_state(registry, strategy)), {"source_id": "sales", "sql": "SELECT 1"}
    )

    assert resp.status_code == 404
    assert b"Unknown data source: sales" in resp.body
    strategy.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_still_serves_the_cache_for_a_live_source() -> None:
    registry = MagicMock()
    registry.get.return_value = DataSourceConfig(
        source_id="sales", name="Sales", source_type=DataSourceType.POSTGRESQL
    )
    strategy = _cache_hit()
    adapter = MCPAdapter(pg_pool=AsyncMock())

    resp = await adapter._execute_query(
        _request(_state(registry, strategy)), {"source_id": "sales", "sql": "SELECT 1"}
    )

    assert resp.status_code == 200
    strategy.get.assert_awaited_once()


def _row(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "name": source_id,
        "source_type": "postgresql",
        "connection_config": {},
        "cache_strategy": "deterministic",
        "enabled": True,
        "metadata": {},
    }


@pytest.mark.asyncio
async def test_a_registry_reload_reports_the_sources_it_stopped_loading() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_row("a"), _row("b")])
    registry = MetadataRegistry(pool)
    await registry.load()

    removed: list[frozenset[str]] = []

    async def listener(ids: frozenset[str]) -> None:
        removed.append(ids)

    async def broken(ids: frozenset[str]) -> None:
        raise RuntimeError("listener failure must not break the reload")

    registry.add_removal_listener(broken)
    registry.add_removal_listener(listener)
    pool.fetch.return_value = [_row("a")]
    await registry.load()

    assert removed == [frozenset({"b"})]
    assert registry.get("b") is None
    # An unchanged reload reports nothing.
    await registry.load()
    assert removed == [frozenset({"b"})]


@pytest.mark.asyncio
async def test_the_gateway_clears_cache_and_pools_for_removed_sources() -> None:
    from interlock.gateway.app import _forget_removed_sources

    invalidator = AsyncMock()
    conn_manager = AsyncMock()
    forget = _forget_removed_sources(cache_invalidator=invalidator, conn_manager=conn_manager)

    await forget(frozenset({"sales", "hr"}))

    assert [c.args[0] for c in invalidator.invalidate_for_source.await_args_list] == [
        "hr",
        "sales",
    ]
    assert [c.args[0] for c in conn_manager.invalidate.await_args_list] == ["hr", "sales"]


def test_admin_and_gateway_share_the_configured_generation_prefix() -> None:
    config = InterLockConfig()
    barrier = SourceGenerationBarrier.for_prefix(None, config.cache.source_generation_prefix)
    assert barrier._key("sales") == f"{config.cache.source_generation_prefix}:sales"
    for app in ("src/interlock/admin/app.py", "src/interlock/gateway/app.py"):
        text = (ROOT / app).read_text()
        assert re.search(
            r"SourceGenerationBarrier\.for_prefix\(\s*\S+,\s*config\.cache\.source_generation_prefix",
            text,
        ), app


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _writer() -> tuple[MagicMock, bytearray]:
    buf = bytearray()
    writer = MagicMock()
    writer.write = lambda data: buf.extend(data)
    writer.drain = AsyncMock()
    return writer, buf


@pytest.mark.asyncio
async def test_an_open_pg_session_ends_when_its_source_is_disabled() -> None:
    registry = MagicMock()
    registry.get.return_value = None
    proxy = PGProxy(listen_port=0, upstream_port=0, registry=registry)
    client_reader = _reader(
        pack_message(MSG_TYPE_QUERY, b"SELECT 1\x00") + pack_message(MSG_TYPE_TERMINATE, b"")
    )
    client_writer, client_buf = _writer()
    upstream_writer, upstream_buf = _writer()

    await proxy._main_loop(
        client_reader, client_writer, _reader(b""), upstream_writer, source_id="sales"
    )

    assert b"3D000" in client_buf
    assert b"Unknown data source: sales" in client_buf
    assert bytes(upstream_buf) == b"", "nothing may reach the upstream"


@pytest.mark.asyncio
async def test_a_prepared_statement_rerun_is_refused_too() -> None:
    """A client re-running a prepared statement sends Bind and Execute, no Parse."""
    registry = MagicMock()
    registry.get.return_value = None
    proxy = PGProxy(listen_port=0, upstream_port=0, registry=registry)
    bind = pack_message("B", b"\x00stmt\x00\x00\x00\x00\x00\x00\x00")
    client_reader = _reader(bind + pack_message(MSG_TYPE_TERMINATE, b""))
    client_writer, client_buf = _writer()
    upstream_writer, upstream_buf = _writer()

    await proxy._main_loop(
        client_reader, client_writer, _reader(b""), upstream_writer, source_id="sales"
    )

    assert b"3D000" in client_buf
    assert bytes(upstream_buf) == b""


@pytest.mark.asyncio
async def test_http_bypass_source_is_never_served_from_the_shared_cache() -> None:
    from interlock.gateway.http_proxy import HTTPProxy

    bypass = AsyncMock()
    bypass.get.return_value = MagicMock(hit=False, data=None, tier=None)
    resolver = MagicMock()
    resolver.for_source.return_value = bypass
    l2 = AsyncMock()
    l2.get.return_value = MagicMock(hit=True, data=b"stale", tier="l2")
    request = MagicMock()
    request.app.state = SimpleNamespace(
        cache_strategies=resolver,
        registry=MagicMock(get=MagicMock(return_value=SimpleNamespace(cache_strategy="bypass"))),
        l2_cache=l2,
    )

    assert await HTTPProxy()._cache_get(request, "key", "public_api") is None
    l2.get.assert_not_awaited()
