"""Cross-protocol cache source-generation write barrier regressions."""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from interlock.core.approval_queue import ApprovalQueue
from interlock.core.normalizer import compute_cache_key
from interlock.gateway.http_proxy import HTTPProxy, _http_cache_key
from interlock.gateway.mcp_adapter import MCPAdapter
from interlock.gateway.pg_messages import (
    MSG_TYPE_COMMAND_COMPLETE,
    MSG_TYPE_QUERY,
    MSG_TYPE_READY_FOR_QUERY,
    pack_message,
)
from interlock.gateway.pg_proxy import PGProxy
from interlock.gateway.pipeline import (
    CacheBarrierUnavailableError,
    GatewayPipeline,
    GatewayRequest,
)
from interlock.models import AgentType, DataSourceConfig, DataSourceType, IdentityContext


@pytest.fixture(autouse=True)
def _safe_test_dns(monkeypatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "upstream.example.com":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", port),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@dataclass(frozen=True)
class _Token:
    source_id: str
    generation: int


class _BarrierInvalidator:
    def __init__(self) -> None:
        self.generation = 0
        self.events: list[str] = []
        self.table_invalidations: list[tuple[str, list[str], int | None]] = []
        self.source_invalidations: list[str] = []

    async def prepare_source_write(self, source_id: str) -> _Token:
        self.generation += 1
        self.events.append(f"prepare:{source_id}:{self.generation}")
        return _Token(source_id=source_id, generation=self.generation)

    async def source_generation(self, source_id: str) -> int:
        self.events.append(f"current:{source_id}:{self.generation}")
        return self.generation

    async def invalidate_for_tables(
        self,
        source_id: str,
        tables: list[str],
        *,
        source_generation: int | None = None,
    ) -> int:
        self.events.append(f"invalidate_tables:{source_id}:{source_generation}")
        self.table_invalidations.append((source_id, tables, source_generation))
        return 0

    async def invalidate_for_source(self, source_id: str) -> int:
        self.events.append(f"invalidate_source:{source_id}")
        self.source_invalidations.append(source_id)
        return 0


def _identity() -> IdentityContext:
    return IdentityContext(
        identity_id=7,
        user="agent",
        agent_type=AgentType.CUSTOM,
        team="eng",
        roles=["writer"],
        session_id="sess-7",
    )


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _writer(events: list[str] | None = None) -> tuple[asyncio.StreamWriter, bytearray]:
    buf = bytearray()
    transport = MagicMock()
    transport.is_closing.return_value = False
    writer = asyncio.StreamWriter(transport, MagicMock(), None, asyncio.get_event_loop())

    def _capture(data: bytes | bytearray | memoryview) -> None:
        if events is not None:
            events.append("upstream")
        buf.extend(data)

    writer.write = _capture  # type: ignore[assignment]
    writer.drain = AsyncMock()  # type: ignore[method-assign]
    writer.close = MagicMock()  # type: ignore[method-assign]
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))  # type: ignore[method-assign]
    return writer, buf


def _query_payload(sql: str) -> bytes:
    return sql.encode("utf-8") + b"\x00"


def _command_complete(tag: str) -> bytes:
    return pack_message(MSG_TYPE_COMMAND_COMPLETE, tag.encode("utf-8") + b"\x00")


def _ready() -> bytes:
    return pack_message(MSG_TYPE_READY_FOR_QUERY, b"I")


@pytest.mark.asyncio
async def test_pipeline_advances_generation_before_direct_write() -> None:
    invalidator = _BarrierInvalidator()
    pipeline = GatewayPipeline(cache_invalidator=invalidator)

    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="http",
            source_id="api1",
            operation="write",
            identity=_identity(),
            method="POST",
            path="/users",
        )
    )

    assert decision.allowed is True
    assert decision.source_generation == 1
    assert decision.audit_metadata["source_generation"] == 1
    assert invalidator.events == ["prepare:api1:1"]


@pytest.mark.asyncio
async def test_pipeline_strict_mode_rejects_without_shared_barrier() -> None:
    invalidator = _BarrierInvalidator()
    pipeline = GatewayPipeline(
        cache_invalidator=invalidator,
        cache_barrier_strict=True,
    )

    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="http",
            source_id="api1",
            operation="write",
            identity=_identity(),
            method="POST",
            path="/users",
        )
    )

    assert decision.allowed is False
    assert decision.status_code == 503
    assert decision.audit_metadata["error_type"] == CacheBarrierUnavailableError.__name__
    assert invalidator.events == []


@pytest.mark.asyncio
async def test_pg_simple_write_advances_before_upstream_and_reuses_generation() -> None:
    invalidator = _BarrierInvalidator()
    proxy = PGProxy(cache_invalidator=invalidator)
    upstream_response = _command_complete("UPDATE 1") + _ready()
    upstream_reader = _reader(upstream_response)
    client_writer, _ = _writer()
    upstream_writer, upstream_buf = _writer(invalidator.events)

    await proxy._handle_simple_query(
        _query_payload("UPDATE users SET name = 'Ada' WHERE id = 1"),
        client_writer,
        upstream_reader,
        upstream_writer,
        identity=_identity(),
        source_id="src1",
    )

    assert bytes(upstream_buf) == pack_message(
        MSG_TYPE_QUERY,
        _query_payload("UPDATE users SET name = 'Ada' WHERE id = 1"),
    )
    assert invalidator.events.index("prepare:src1:1") < invalidator.events.index("upstream")
    assert invalidator.table_invalidations == [("src1", ["users"], 1)]
    assert invalidator.source_invalidations == []


@respx.mock
def test_http_write_advances_before_upstream_and_reuses_generation() -> None:
    invalidator = _BarrierInvalidator()
    registry = MagicMock()
    registry.get.return_value = DataSourceConfig(
        source_id="api1",
        name="API",
        source_type=DataSourceType.HTTP,
        connection_config={"base_url": "https://upstream.example.com/v1"},
    )
    proxy = HTTPProxy()
    app = Starlette(routes=proxy.get_routes())
    app.state.registry = registry
    app.state.cache_invalidator = invalidator
    app.state._proxy = proxy
    proxy._client = httpx.AsyncClient()

    def _upstream(request: httpx.Request) -> httpx.Response:
        invalidator.events.append("upstream")
        return httpx.Response(201, json={"id": 1})

    respx.post("https://upstream.example.com/v1/users").mock(side_effect=_upstream)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/proxy/api1/users", json={"name": "Ada"})

    assert resp.status_code == 201
    assert invalidator.events[:2] == ["prepare:api1:1", "upstream"]
    assert invalidator.table_invalidations == [("api1", [], 1)]
    assert invalidator.source_invalidations == []


@pytest.mark.asyncio
async def test_mcp_write_advances_before_upstream_and_reuses_generation() -> None:
    invalidator = _BarrierInvalidator()
    pool = AsyncMock()

    async def _fetch(sql: str):
        invalidator.events.append("upstream")
        return []

    pool.fetch.side_effect = _fetch
    state = SimpleNamespace(
        cache_invalidator=invalidator,
        cache_strategy=None,
        audit_logger=None,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/tools/call",
            "headers": [],
            "query_string": b"",
            "app": SimpleNamespace(state=state),
            "state": {},
        }
    )
    request.state.identity = _identity()

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(
        request,
        {"sql": "UPDATE users SET name = 'Ada' WHERE id = 1", "source_id": "src1"},
    )

    assert resp.status_code == 200
    assert invalidator.events.index("prepare:src1:1") < invalidator.events.index("upstream")
    assert invalidator.table_invalidations == [("src1", ["users"], 1)]
    assert invalidator.source_invalidations == []


@pytest.mark.asyncio
async def test_approved_sql_write_advances_before_execution_and_reuses_generation() -> None:
    invalidator = _BarrierInvalidator()
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")
    pool.fetchrow = AsyncMock(
        return_value={
            "id": 9,
            "identity_id": 7,
            "source_id": "src1",
            "sql_text": "UPDATE users SET name = 'Ada' WHERE id = 1",
            "risk_level": "medium",
            "status": "approved",
            "request_metadata": json.dumps({"protocol": "postgresql"}),
        }
    )
    origin_pool = AsyncMock()

    async def _execute(sql: str) -> str:
        invalidator.events.append("upstream")
        return "UPDATE 1"

    origin_pool.execute.side_effect = _execute
    conn_mgr = AsyncMock()
    conn_mgr.get_pool.return_value = origin_pool

    queue = ApprovalQueue(pool, conn_mgr, cache_invalidator=invalidator)
    result = await queue.approve(9, "reviewer")

    assert result == {"status": "approved", "executed": True}
    assert invalidator.events[:2] == ["prepare:src1:1", "upstream"]
    assert invalidator.table_invalidations == [("src1", ["users"], 1)]
    assert invalidator.source_invalidations == []


def test_deterministic_read_keys_include_source_generation() -> None:
    sql_key_1 = compute_cache_key(
        "src1", "SELECT * FROM users", source_generation=1, protocol="postgresql"
    )
    sql_key_2 = compute_cache_key(
        "src1", "SELECT * FROM users", source_generation=2, protocol="postgresql"
    )
    http_key_1 = _http_cache_key(
        source_id="api1",
        method="GET",
        path="/users",
        query="",
        identity_id=7,
        role="reader",
        team="eng",
        grants_version="1",
        governance_scope_hash="scope-a",
        source_generation=1,
    )
    http_key_2 = _http_cache_key(
        source_id="api1",
        method="GET",
        path="/users",
        query="",
        identity_id=7,
        role="reader",
        team="eng",
        grants_version="1",
        governance_scope_hash="scope-a",
        source_generation=2,
    )

    assert sql_key_1 != sql_key_2
    assert http_key_1 != http_key_2
