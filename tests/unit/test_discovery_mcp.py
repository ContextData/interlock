"""Unit tests for the MCP discover tool."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from interlock.gateway.mcp_adapter import MCP_TOOLS, MCPAdapter
from interlock.models import AgentType, IdentityContext, SearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(adapter: MCPAdapter) -> Starlette:
    return Starlette(routes=adapter.get_routes())


def _mock_discovery(results: list[SearchResult] | None = None, error: Exception | None = None):
    """Return a mock DiscoverySearch."""
    ds = MagicMock()
    if error:
        ds.search = AsyncMock(side_effect=error)
    else:
        ds.search = AsyncMock(return_value=results or [])
    return ds


def _make_result(asset_id: int = 1, score: float = 3.0) -> SearchResult:
    return SearchResult(
        asset_id=asset_id,
        source_id="src1",
        asset_type="document",
        asset_path=f"/doc/{asset_id}",
        title=f"Doc {asset_id}",
        summary=f"Summary for doc {asset_id}",
        score=score,
        strategy="vector",
    )


def _identity_with_grants(*source_ids: str) -> IdentityContext:
    return IdentityContext(
        identity_id=7,
        user="agent",
        agent_type=AgentType.CUSTOM,
        source_roles=[
            {"source_id": source_id, "role": "reader", "role_id": idx + 1}
            for idx, source_id in enumerate(source_ids)
        ],
    )


# ---------------------------------------------------------------------------
# Tests: discover tool in tools/list
# ---------------------------------------------------------------------------


class TestDiscoverToolListed:
    def test_discover_in_tools_list(self):
        names = {t["name"] for t in MCP_TOOLS}
        assert "agentgate_discover" in names
        assert "agentgate_related_documents" in names

    def test_discover_tool_schema(self):
        discover_tool = next(t for t in MCP_TOOLS if t["name"] == "agentgate_discover")
        assert "query" in discover_tool["inputSchema"]["properties"]
        assert "query" in discover_tool["inputSchema"]["required"]

    def test_tools_list_endpoint_includes_discover(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post("/mcp/tools/list")
        assert resp.status_code == 200
        tool_names = {t["name"] for t in resp.json()["tools"]}
        assert "agentgate_discover" in tool_names


# ---------------------------------------------------------------------------
# Tests: discover tool call
# ---------------------------------------------------------------------------


class TestDiscoverToolCall:
    def test_discover_returns_results(self):
        results = [_make_result(1, 3.0), _make_result(2, 1.5)]
        ds = _mock_discovery(results)

        adapter = MCPAdapter(discovery_search=ds)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={
                "name": "discover",
                "arguments": {
                    "source_id": "src1",
                    "query": "test query",
                    "limit": 5,
                },
            },
        )
        assert resp.status_code == 200

        data = resp.json()
        text = data["content"][0]["text"]
        parsed = json.loads(text)
        assert len(parsed) == 2
        assert parsed[0]["asset_id"] == 1
        assert parsed[0]["score"] == 3.0
        assert parsed[1]["title"] == "Doc 2"

        ds.search.assert_called_once_with(query="test query", source_id="src1", limit=5)

    def test_discover_with_source_id(self):
        ds = _mock_discovery([])
        adapter = MCPAdapter(discovery_search=ds)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={
                "name": "discover",
                "arguments": {"query": "stuff", "source_id": "mydb"},
            },
        )
        assert resp.status_code == 200
        ds.search.assert_called_once_with(query="stuff", source_id="mydb", limit=10)

    def test_discover_no_search_returns_503(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={
                "name": "discover",
                "arguments": {"source_id": "src1", "query": "hello"},
            },
        )
        assert resp.status_code == 503

    def test_discover_error_returns_400(self):
        ds = _mock_discovery(error=Exception("index offline"))
        adapter = MCPAdapter(discovery_search=ds)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={
                "name": "discover",
                "arguments": {"source_id": "src1", "query": "test"},
            },
        )
        assert resp.status_code == 400
        assert "Error" in resp.json()["content"][0]["text"]

    def test_discover_filters_results_to_active_source_grants(self):
        ds = _mock_discovery(
            [
                _make_result(1, 3.0),
                SearchResult(
                    asset_id=2,
                    source_id="src2",
                    asset_type="document",
                    asset_path="/doc/2",
                    title="Doc 2",
                    score=2.0,
                    strategy="vector",
                ),
            ]
        )
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=_identity_with_grants("src1"))
        adapter = MCPAdapter(discovery_search=ds)
        app = _make_app(adapter)
        app.state.auth_manager = auth
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            headers={"Authorization": "Bearer valid"},
            json={
                "name": "discover",
                "arguments": {"source_id": "src1", "query": "test"},
            },
        )

        assert resp.status_code == 200
        parsed = json.loads(resp.json()["content"][0]["text"])
        assert [item["source_id"] for item in parsed] == ["src1"]
