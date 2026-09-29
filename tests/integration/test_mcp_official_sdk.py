"""Wire-level compatibility with the official MCP Python SDK modern client."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from interlock.gateway.mcp_adapter import (
    MCP_HANDSHAKE_PROTOCOL_VERSIONS,
    MCP_PROTOCOL_VERSION,
    MCPAdapter,
)

mcp = pytest.importorskip("mcp", reason="install the mcp-certification extra")
from mcp import Client  # noqa: E402


@pytest.fixture
def mcp_server_url() -> Iterator[str]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])

    from starlette.applications import Starlette

    app = Starlette(routes=MCPAdapter().get_routes())
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("MCP certification server did not start")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.protocol
async def test_official_mcp_sdk_discovers_and_lists_interlock_tools(
    mcp_server_url: str,
) -> None:
    async with Client(mcp_server_url, mode="auto") as client:
        assert client.protocol_version == MCP_PROTOCOL_VERSION
        assert client.server_info is not None
        assert client.server_info.name == "interlock"

        result = await client.list_tools()

    names = {tool.name for tool in result.tools}
    assert "interlock_query" in names
    assert "interlock_discover" in names
    assert "agentgate_query" in names


@pytest.mark.protocol
async def test_official_mcp_sdk_legacy_initialize_handshake(mcp_server_url: str) -> None:
    """The handshake every mainstream client uses must work.

    `mode="auto"` probes `server/discover` first and only falls back, so it
    could pass while the `initialize` path stayed broken - which it was. This
    forces the legacy path, the one Claude Code and Claude Desktop take.
    """
    async with Client(mcp_server_url, mode="legacy") as client:
        assert client.protocol_version in MCP_HANDSHAKE_PROTOCOL_VERSIONS
        assert client.server_info is not None
        assert client.server_info.name == "interlock"

        result = await client.list_tools()

    assert "interlock_query" in {tool.name for tool in result.tools}


@pytest.mark.protocol
async def test_official_mcp_sdk_legacy_session_pings_and_calls_a_tool(
    mcp_server_url: str,
) -> None:
    """`ping` and a governed tool call over the negotiated 2025 session.

    Driven through the SDK's public client rather than its transport
    internals: `streamablehttp_client` was removed in mcp 2.0, and a test
    written against private plumbing would break on an SDK upgrade while
    proving nothing extra.

    This fixture wires no auth manager, so `interlock_list_sources` correctly
    returns an empty catalogue rather than an error - the assertion is that
    the call round-trips as a well-formed tool result over a negotiated 2025
    session, which is what `initialize` had to succeed for. Denials and
    queued writes are covered where a pipeline can be substituted, in
    tests/unit/test_mcp_adapter.py.
    """
    async with Client(mcp_server_url, mode="legacy") as client:
        assert client.protocol_version in MCP_HANDSHAKE_PROTOCOL_VERSIONS

        await client.send_ping()

        result = await client.call_tool("interlock_list_sources", {})

    assert result.is_error is False
    assert json.loads(result.content[0].text) == {}
