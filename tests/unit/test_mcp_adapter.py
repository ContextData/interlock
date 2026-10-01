"""Unit tests for the MCP protocol adapter."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from interlock.errors import DataSourceNotFoundError, DataSourceUnavailableError
from interlock.gateway import mcp_adapter
from interlock.gateway.mcp_adapter import (
    MCP_HANDSHAKE_PROTOCOL_VERSIONS,
    MCP_PROTOCOL_VERSION,
    MCP_TOOLS,
    MCPAdapter,
    _allowed_source_ids_for_identity,
)
from interlock.gateway.pipeline import GatewayDecision
from interlock.models import AgentType, DataSourceConfig, DataSourceType, IdentityContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(adapter: MCPAdapter) -> Starlette:
    """Wrap an MCPAdapter in a minimal Starlette app for testing."""
    return Starlette(routes=adapter.get_routes())


class FakeRecord(dict):
    """Dict subclass that mimics asyncpg.Record for dict() conversion."""

    pass


def _make_records(rows: list[dict]) -> list[FakeRecord]:
    """Convert plain dicts to FakeRecord instances."""
    return [FakeRecord(row) for row in rows]


def _mock_pool(rows: list[dict] | None = None, error: Exception | None = None):
    """Return an asyncpg-like pool mock."""
    pool = MagicMock()
    if error is not None:
        pool.fetch = AsyncMock(side_effect=error)
    else:
        pool.fetch = AsyncMock(return_value=_make_records(rows or []))
    return pool


def _mock_registry(sources: dict[str, DataSourceConfig] | None = None):
    """Return a MetadataRegistry mock."""
    registry = MagicMock()
    registry.get_all.return_value = sources or {}
    return registry


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


def _identity_with_source_roles(source_roles: list[dict]) -> IdentityContext:
    return IdentityContext(
        identity_id=7,
        user="agent",
        agent_type=AgentType.CUSTOM,
        source_roles=source_roles,
    )


def _streamable_headers(method: str, *, name: str | None = None, sse: bool = False) -> dict:
    headers = {
        "Accept": (
            "text/event-stream, application/json" if sse else "application/json, text/event-stream"
        ),
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _streamable_params(**values) -> dict:
    return {
        **values,
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "interlock-tests", "version": "1"},
        },
    }


# ---------------------------------------------------------------------------
# Tests: list_tools
# ---------------------------------------------------------------------------


class TestListTools:
    def test_get_returns_tools(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.get("/mcp/tools/list")
        assert resp.status_code == 200
        data = resp.json()
        assert "tools" in data
        # Five tools, each with a deprecated `agentgate_` alias, plus
        # `interlock_describe_access`, which has none.
        assert len(data["tools"]) == len(MCP_TOOLS) == 11

    def test_post_returns_tools(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post("/mcp/tools/list")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tools"] == MCP_TOOLS

    def test_tool_names(self):
        names = {t["name"] for t in MCP_TOOLS}
        assert {
            "interlock_query",
            "interlock_list_sources",
            "interlock_describe_source",
            "interlock_discover",
            "interlock_related_documents",
        }.issubset(names)
        assert names == {
            "interlock_query",
            "interlock_list_sources",
            "interlock_describe_source",
            "interlock_discover",
            "interlock_related_documents",
            # Added after the alias loop, so it has no `agentgate_` twin - there
            # is no deprecated surface to be compatible with.
            "interlock_describe_access",
            "agentgate_query",
            "agentgate_list_sources",
            "agentgate_describe_source",
            "agentgate_discover",
            "agentgate_related_documents",
        }

    def test_tool_schemas_have_required_fields(self):
        for tool in MCP_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"


class TestListSourcesAudit:
    def test_list_sources_emits_success_audit(self):
        sources = {
            "src1": DataSourceConfig(
                source_id="src1",
                name="Source One",
                source_type=DataSourceType.POSTGRESQL,
            )
        }
        registry = _mock_registry(sources)
        adapter = MCPAdapter(registry=registry)
        app = _make_app(adapter)
        app.state.audit_logger = AsyncMock()
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "list_sources", "arguments": {}},
        )

        assert resp.status_code == 200
        app.state.audit_logger.log.assert_awaited_once()
        record = app.state.audit_logger.log.await_args.args[0]
        assert record.protocol == "mcp"
        assert record.source_id == "__registry__"
        assert record.route == "agentgate_list_sources"
        assert record.request_metadata["row_count"] == 1


# ---------------------------------------------------------------------------
# Tests: call_tool - query
# ---------------------------------------------------------------------------


class TestDescribeAccess:
    """An agent can ask what it may do instead of finding out by being refused.

    Before this tool, `list_sources` gave a name and a type and nothing about
    authority, so the only way to learn a boundary was to cross it and read the
    denial - a wasted turn each time, and a signal a weaker model reads as a
    malfunction worth retrying.
    """

    @staticmethod
    def _app_with_access(access, *, identity_id=7, granted=("sales_pg", "hr_mysql")):
        adapter = MCPAdapter(registry=_mock_registry({}))
        app = _make_app(adapter)
        app.state.audit_logger = AsyncMock()
        evaluator = AsyncMock()
        evaluator.describe_access = AsyncMock(return_value=access)
        app.state.source_role_evaluator = evaluator
        identity = SimpleNamespace(
            identity_id=identity_id,
            name="analyst",
            source_roles=[{"source_id": s, "role": "analyst"} for s in granted],
        )
        return app, evaluator, identity

    def _call(self, app, identity, arguments=None):
        async def inject_identity(request, call_next):
            request.state.identity = identity
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=inject_identity)
        client = TestClient(app)
        return client.post(
            "/mcp/tools/call",
            json={"name": "interlock_describe_access", "arguments": arguments or {}},
            headers={"Authorization": "Bearer any-token"},
        )

    def test_it_answers_for_every_granted_source(self):
        access = {
            "roles": ["analyst"],
            "allow": [{"action": "db.table.select", "resource": "*.*"}],
            "deny": [],
        }
        app, evaluator, identity = self._app_with_access(access)

        resp = self._call(app, identity)

        assert resp.status_code == 200
        payload = json.loads(resp.json()["content"][0]["text"])
        assert set(payload) == {"sales_pg", "hr_mysql"}
        assert payload["sales_pg"]["allow"][0]["action"] == "db.table.select"

    def test_source_id_narrows_the_answer(self):
        access = {"roles": ["analyst"], "allow": [], "deny": []}
        app, evaluator, identity = self._app_with_access(access)

        resp = self._call(app, identity, {"source_id": "sales_pg"})

        payload = json.loads(resp.json()["content"][0]["text"])
        assert set(payload) == {"sales_pg"}

    def test_it_never_describes_a_source_the_caller_was_not_granted(self):
        """The whole value of the tool depends on this: it reports the caller's
        own authority, so asking about someone else's source reveals nothing."""
        access = {"roles": ["analyst"], "allow": [], "deny": []}
        app, evaluator, identity = self._app_with_access(access, granted=("sales_pg",))

        resp = self._call(app, identity, {"source_id": "payroll_pg"})

        assert resp.status_code == 200
        assert json.loads(resp.json()["content"][0]["text"]) == {}
        evaluator.describe_access.assert_not_awaited()

    def test_it_is_audited_against_the_registry_like_list_sources(self):
        access = {"roles": ["analyst"], "allow": [], "deny": []}
        app, _, identity = self._app_with_access(access, granted=("sales_pg",))

        self._call(app, identity)

        record = app.state.audit_logger.log.await_args.args[0]
        assert record.source_id == "__registry__"
        assert record.route == "interlock_describe_access"
        assert record.request_metadata["row_count"] == 1

    def test_without_a_resolved_identity_it_refuses_rather_than_implying_nothing(self):
        """An empty object would read as "you may do nothing", which is a
        different and misleading claim from "this cannot be answered"."""
        adapter = MCPAdapter(registry=_mock_registry({}))
        app = _make_app(adapter)
        app.state.audit_logger = AsyncMock()
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "interlock_describe_access", "arguments": {}},
            headers={"Authorization": "Bearer any-token"},
        )

        assert resp.status_code == 403
        assert "unavailable" in resp.json()["content"][0]["text"]

    def test_the_tool_is_advertised_without_a_deprecated_alias(self):
        names = {tool["name"] for tool in MCP_TOOLS}
        assert "interlock_describe_access" in names
        assert "agentgate_describe_access" not in names


class TestRequiredToolArguments:
    """A malformed call is a malformed call, not a governance decision.

    An `interlock_query` without `sql` used to reach the governance layer, where
    the empty statement made `build_permission_request` derive `db.execute_raw`
    on `*`. No correctly scoped source role allows that, so the agent was told
    "Source role denied" and its operator went looking at role configuration for
    what was really a missing argument.
    """

    @pytest.mark.parametrize(
        "arguments", ({"source_id": "default"}, {"source_id": "default", "sql": "   "})
    )
    def test_a_query_without_sql_is_rejected_before_governance(self, arguments):
        pool = _mock_pool([])
        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        app.state.source_role_evaluator = AsyncMock()
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "interlock_query", "arguments": arguments},
        )

        assert resp.status_code == 400
        assert resp.json() == {"error": "interlock_query requires sql"}
        # The whole point: no role was evaluated and no upstream was touched.
        app.state.source_role_evaluator.evaluate.assert_not_awaited()
        pool.fetch.assert_not_called()

    def test_the_error_names_the_tool_the_caller_asked_for(self):
        """Not the deprecated alias it is canonicalised to internally."""
        adapter = MCPAdapter(pg_pool=_mock_pool([]))
        client = TestClient(_make_app(adapter))

        for name in ("interlock_query", "agentgate_query"):
            resp = client.post(
                "/mcp/tools/call",
                json={"name": name, "arguments": {"source_id": "default"}},
            )
            assert resp.status_code == 400
            assert resp.json()["error"] == f"{name} requires sql"

    def test_a_missing_source_id_still_names_the_requested_tool(self):
        """A bearer header is needed to get this far: an anonymous caller with no
        source_id is answered 401 first, which is the right order - an
        unauthenticated client should not be taught the argument shape."""
        adapter = MCPAdapter(pg_pool=_mock_pool([]))
        client = TestClient(_make_app(adapter))

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "interlock_query", "arguments": {"sql": "SELECT 1"}},
            headers={"Authorization": "Bearer any-token"},
        )

        assert resp.status_code == 400
        assert resp.json() == {"error": "interlock_query requires source_id"}


class TestQueryTool:
    def test_query_success(self):
        rows = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        pool = _mock_pool(rows)

        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "default", "sql": "SELECT 1"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert data["content"][0]["type"] == "text"
        # The fetch was called with the SQL
        pool.fetch.assert_called_once_with("SELECT 1")

    def test_query_with_datetime_serialization(self):
        """Datetime values should be serialized to ISO format."""
        dt = datetime(2025, 1, 15, 10, 30, 0)
        pool = _mock_pool([{"ts": dt}])

        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "x", "sql": "SELECT now()"}},
        )
        assert resp.status_code == 200
        text = resp.json()["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed[0]["ts"] == "2025-01-15T10:30:00"

    def test_query_error_returns_400(self):
        pool = _mock_pool(error=Exception("syntax error"))

        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "x", "sql": "BAD SQL"}},
        )
        assert resp.status_code == 400
        text = resp.json()["content"][0]["text"]
        assert "Error" in text
        assert "syntax error" not in text

    def test_query_no_pool_returns_503(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": {"source_id": "x", "sql": "SELECT 1"}},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Tests: call_tool - list_sources
# ---------------------------------------------------------------------------


class TestListSourcesTool:
    def test_list_sources_with_registry(self):
        sources = {
            "src1": DataSourceConfig(
                source_id="src1",
                name="Source One",
                source_type=DataSourceType.POSTGRESQL,
            ),
            "src2": DataSourceConfig(
                source_id="src2",
                name="Source Two",
                source_type=DataSourceType.HTTP,
            ),
        }
        registry = _mock_registry(sources)
        adapter = MCPAdapter(registry=registry)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "list_sources", "arguments": {}},
        )
        assert resp.status_code == 200
        text = resp.json()["content"][0]["text"]
        parsed = json.loads(text)
        assert "src1" in parsed
        assert parsed["src1"]["name"] == "Source One"
        assert parsed["src1"]["type"] == "postgresql"
        assert parsed["src2"]["type"] == "http"

    def test_list_sources_excludes_explicit_blocked_grants(self):
        sources = {
            "allowed": DataSourceConfig(
                source_id="allowed",
                name="Allowed Source",
                source_type=DataSourceType.POSTGRESQL,
            ),
            "blocked": DataSourceConfig(
                source_id="blocked",
                name="Blocked Source",
                source_type=DataSourceType.HTTP,
            ),
        }
        registry = _mock_registry(sources)
        adapter = MCPAdapter(registry=registry)
        app = _make_app(adapter)

        async def inject_identity(request, call_next):
            request.state.identity = _identity_with_source_roles(
                [
                    {
                        "source_id": "allowed",
                        "role": "reader",
                        "permissions": [{"effect": "allow", "action": "discovery.search"}],
                    },
                    {
                        "source_id": "blocked",
                        "role": "blocked",
                        "permissions": [{"effect": "deny", "action": "*"}],
                    },
                ]
            )
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=inject_identity)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "list_sources", "arguments": {}},
        )

        assert resp.status_code == 200
        parsed = json.loads(resp.json()["content"][0]["text"])
        assert "allowed" in parsed
        assert "blocked" not in parsed

    def test_list_sources_no_registry(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "list_sources", "arguments": {}},
        )
        assert resp.status_code == 200
        text = resp.json()["content"][0]["text"]
        assert json.loads(text) == {}


def test_allowed_source_ids_excludes_deny_only_grants() -> None:
    identity = _identity_with_source_roles(
        [
            {"source_id": "readable", "role": "reader"},
            {"source_id": "blocked", "role": "blocked"},
            {
                "source_id": "deny_only",
                "role": "custom",
                "permissions": [{"effect": "deny", "action": "*"}],
            },
        ]
    )

    assert _allowed_source_ids_for_identity(identity) == {"readable"}


# ---------------------------------------------------------------------------
# Tests: call_tool - describe_source
# ---------------------------------------------------------------------------


class TestDescribeSourceTool:
    def test_describe_source_success(self):
        rows = [
            {
                "table_schema": "public",
                "table_name": "users",
                "column_name": "id",
                "data_type": "integer",
            },
        ]
        pool = _mock_pool(rows)

        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "describe_source", "arguments": {"source_id": "default"}},
        )
        assert resp.status_code == 200
        text = resp.json()["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed[0]["table_name"] == "users"

    def test_describe_source_error(self):
        pool = _mock_pool(error=Exception("connection refused"))

        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "describe_source", "arguments": {"source_id": "x"}},
        )
        assert resp.status_code == 400
        assert "Error" in resp.json()["content"][0]["text"]

    def test_describe_source_no_pool(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "describe_source", "arguments": {"source_id": "x"}},
        )
        assert resp.status_code == 503

    def test_describe_source_emits_success_audit(self):
        rows = [
            {
                "table_schema": "public",
                "table_name": "customers",
                "column_name": "email",
                "data_type": "text",
            }
        ]
        pool = _mock_pool(rows)
        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        app.state.audit_logger = AsyncMock()
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "describe_source", "arguments": {"source_id": "src1"}},
        )

        assert resp.status_code == 200
        app.state.audit_logger.log.assert_awaited_once()
        record = app.state.audit_logger.log.await_args.args[0]
        assert record.protocol == "mcp"
        assert record.source_id == "src1"
        assert record.route == "agentgate_describe_source"
        assert record.request_metadata["row_count"] == 1


# ---------------------------------------------------------------------------
# Tests: unknown tool
# ---------------------------------------------------------------------------


class TestUnknownTool:
    def test_unknown_tool_returns_404(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "nonexistent_tool", "arguments": {}},
        )
        assert resp.status_code == 404
        assert "Unknown tool" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Tests: ingress hardening
# ---------------------------------------------------------------------------


class TestMCPIngressHardening:
    def test_invalid_json_returns_deterministic_400(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            content=b'{"name":',
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid_json"}

    def test_oversized_request_returns_413_before_handler(self):
        pool = _mock_pool([{"id": 1}])
        adapter = MCPAdapter(pg_pool=pool, max_request_bytes=8)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            content=b"x" * 9,
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 413
        assert resp.json() == {"error": "request_too_large"}
        pool.fetch.assert_not_called()

    def test_non_object_arguments_return_422(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={"name": "query", "arguments": []},
        )

        assert resp.status_code == 422
        assert resp.json() == {"error": "invalid_arguments"}

    def test_discovery_limit_is_clamped_before_search(self):
        discovery = MagicMock()
        discovery.search = AsyncMock(return_value=[])
        adapter = MCPAdapter(discovery_search=discovery, max_tool_limit=5)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={
                "name": "agentgate_discover",
                "arguments": {
                    "source_id": "source-1",
                    "query": "customers",
                    "limit": 500,
                },
            },
        )

        assert resp.status_code == 200
        discovery.search.assert_awaited_once()
        assert discovery.search.await_args.kwargs["limit"] == 5

    def test_invalid_limit_returns_422_before_search(self):
        discovery = MagicMock()
        discovery.search = AsyncMock(return_value=[])
        adapter = MCPAdapter(discovery_search=discovery)
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            json={
                "name": "agentgate_discover",
                "arguments": {"query": "customers", "limit": "many"},
            },
        )

        assert resp.status_code == 422
        assert resp.json() == {"error": "invalid_limit"}
        discovery.search.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: route structure
# ---------------------------------------------------------------------------


class TestRoutes:
    def test_get_routes_returns_standard_and_legacy_routes(self):
        adapter = MCPAdapter()
        routes = adapter.get_routes()
        assert len(routes) == 4

    def test_route_paths(self):
        adapter = MCPAdapter()
        routes = adapter.get_routes()
        paths = {r.path for r in routes}
        assert paths == {"/mcp", "/mcp/tools/list", "/mcp/tools/call", "/mcp/sse"}

    def test_sse_returns_tool_catalogue_event(self):
        adapter = MCPAdapter()
        app = _make_app(adapter)
        client = TestClient(app)

        resp = client.get("/mcp/sse")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert "event: tools" in resp.text
        assert "agentgate_query" in resp.text


class TestStreamableHTTP:
    def test_server_discover_advertises_modern_protocol_and_capabilities(self):
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_streamable_headers("server/discover"),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": _streamable_params(),
            },
        )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == 1
        assert payload["result"]["resultType"] == "complete"
        assert payload["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
        assert payload["result"]["capabilities"]["tools"]["listChanged"] is False
        server_info = payload["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]
        assert server_info["name"] == "interlock"

    def test_tools_list_uses_jsonrpc_envelope(self):
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_streamable_headers("tools/list"),
            json={
                "jsonrpc": "2.0",
                "id": "tools",
                "method": "tools/list",
                "params": _streamable_params(),
            },
        )

        assert resp.status_code == 200
        assert resp.json()["result"]["tools"] == MCP_TOOLS
        assert resp.json()["result"]["resultType"] == "complete"

    def test_tools_call_delegates_to_governed_handler(self):
        registry = _mock_registry({})
        client = TestClient(_make_app(MCPAdapter(registry=registry)))

        resp = client.post(
            "/mcp",
            headers=_streamable_headers("tools/call", name="interlock_list_sources"),
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": _streamable_params(
                    name="interlock_list_sources",
                    arguments={},
                ),
            },
        )

        assert resp.status_code == 200
        content = resp.json()["result"]["content"]
        assert json.loads(content[0]["text"]) == {}

    def test_request_scoped_sse_response(self):
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_streamable_headers("tools/list", sse=True),
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/list",
                "params": _streamable_params(),
            },
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert "event: message" in resp.text
        assert '"id": 8' in resp.text

    def test_cross_origin_browser_request_is_rejected(self):
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers={"Origin": "https://attacker.example"},
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
        )

        assert resp.status_code == 403
        assert resp.json()["error"]["message"] == "Origin not allowed"

    def test_matching_spoofed_host_and_origin_is_not_trusted(self):
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers={
                **_streamable_headers("tools/list"),
                "Host": "attacker.example",
                "Origin": "https://attacker.example",
            },
            json={
                "jsonrpc": "2.0",
                "id": 91,
                "method": "tools/list",
                "params": _streamable_params(),
            },
        )

        assert resp.status_code == 403

    def test_exact_configured_browser_origin_is_allowed(self):
        app = _make_app(MCPAdapter())
        app.state.mcp_allowed_origins = ("https://console.example",)
        client = TestClient(app)

        resp = client.post(
            "/mcp",
            headers={
                **_streamable_headers("tools/list"),
                "Origin": "https://console.example",
            },
            json={
                "jsonrpc": "2.0",
                "id": 92,
                "method": "tools/list",
                "params": _streamable_params(),
            },
        )

        assert resp.status_code == 200

    def test_notification_returns_empty_accepted_response(self):
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_streamable_headers("notifications/cancelled"),
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": _streamable_params(requestId="obsolete"),
            },
        )

        assert resp.status_code == 202
        assert resp.content == b""

    def test_missing_protocol_version_header_is_served_as_the_2025_dialect(self):
        """The 2025-06-18 spec tells servers to assume 2025-03-26 here.

        This used to answer -32020. Real clients omit the header on the very
        first request - the python SDK clears it before `initialize`, and
        Claude Code only sets it once a version has been negotiated - so
        rejecting it made the server unreachable to every handshake-era
        client. The 2026 dialect still requires the header and its mirrors;
        that is pinned separately below.
        """
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
        )

        assert resp.status_code == 200
        assert resp.json()["result"]["tools"] == MCP_TOOLS

    def test_unsupported_protocol_version_uses_mcp_error_contract(self):
        client = TestClient(_make_app(MCPAdapter()))
        params = _streamable_params()
        params["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2099-01-01"

        resp = client.post(
            "/mcp",
            headers={
                **_streamable_headers("tools/list"),
                "MCP-Protocol-Version": "2099-01-01",
            },
            json={"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": params},
        )

        assert resp.status_code == 400
        assert resp.json()["error"] == {
            "code": -32022,
            "message": "UnsupportedProtocolVersionError",
            "data": {
                "supported": sorted([*MCP_HANDSHAKE_PROTOCOL_VERSIONS, MCP_PROTOCOL_VERSION]),
                "requested": "2099-01-01",
            },
        }

    def test_initialize_and_ping_are_not_supported_in_modern_protocol(self):
        client = TestClient(_make_app(MCPAdapter()))

        for request_id, method in enumerate(("initialize", "ping"), start=20):
            resp = client.post(
                "/mcp",
                headers=_streamable_headers(method),
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": _streamable_params(),
                },
            )
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == -32601

    def test_client_capabilities_are_required_per_request(self):
        client = TestClient(_make_app(MCPAdapter()))
        params = _streamable_params()
        del params["_meta"]["io.modelcontextprotocol/clientCapabilities"]

        resp = client.post(
            "/mcp",
            headers=_streamable_headers("tools/list"),
            json={"jsonrpc": "2.0", "id": 12, "method": "tools/list", "params": params},
        )

        assert resp.status_code == 400
        assert resp.json()["error"] == {"code": -32020, "message": "HeaderMismatch"}

    def test_mirrored_method_and_name_must_match_body(self):
        client = TestClient(_make_app(MCPAdapter()))
        headers = _streamable_headers("tools/call", name="different_tool")

        resp = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": _streamable_params(name="interlock_list_sources", arguments={}),
            },
        )

        assert resp.status_code == 400
        assert resp.json()["error"] == {"code": -32020, "message": "HeaderMismatch"}


class TestRelatedDocumentsTool:
    def test_related_documents_filters_to_seed_asset_source(self):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={"source_id": "src1"})
        pool.fetch = AsyncMock(
            return_value=_make_records(
                [
                    {
                        "asset_id": 2,
                        "source_id": "src1",
                        "asset_type": "document",
                        "asset_path": "/doc/2",
                        "title": "Related",
                        "summary": "Same source",
                        "shared_entity_count": 1,
                    }
                ]
            )
        )
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=_identity_with_grants("src1"))
        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        app.state.auth_manager = auth
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            headers={"Authorization": "Bearer valid"},
            json={
                "name": "agentgate_related_documents",
                "arguments": {"asset_id": 1, "limit": 5},
            },
        )

        assert resp.status_code == 200
        parsed = json.loads(resp.json()["content"][0]["text"])
        assert [row["source_id"] for row in parsed] == ["src1"]
        pool.fetch.assert_called_once()
        assert pool.fetch.call_args.args[3] == "src1"

    def test_related_documents_denies_seed_asset_without_active_grant(self):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={"source_id": "src1"})
        pool.fetch = AsyncMock(return_value=[])
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=_identity_with_grants("src2"))
        adapter = MCPAdapter(pg_pool=pool)
        app = _make_app(adapter)
        app.state.auth_manager = auth
        client = TestClient(app)

        resp = client.post(
            "/mcp/tools/call",
            headers={"Authorization": "Bearer valid"},
            json={
                "name": "agentgate_related_documents",
                "arguments": {"asset_id": 1, "limit": 5},
            },
        )

        assert resp.status_code == 403
        assert "not permitted" in resp.json()["content"][0]["text"]
        pool.fetch.assert_not_called()


def _handshake_headers(version: str | None = "2025-11-25", *, sse: bool = False) -> dict:
    """Headers a 2025-era client actually sends: no mirrors, often no version.

    Verified against the installed clients rather than assumed. The python SDK
    clears the cached version before `initialize` and sets it afterwards;
    Claude Code sets `mcp-protocol-version` only once one has been negotiated.
    Neither sends `Mcp-Method`, `Mcp-Name`, or the `_meta` mirrors.
    """
    headers = {
        "Accept": (
            "text/event-stream, application/json" if sse else "application/json, text/event-stream"
        )
    }
    if version is not None:
        headers["MCP-Protocol-Version"] = version
    return headers


class TestHandshakeDialect:
    """MCP 2025-03-26 / 2025-06-18 / 2025-11-25 clients must be able to connect.

    Every mainstream MCP client - Claude Code, Claude Desktop, the official
    python SDK's legacy path - opens with `initialize` and speaks a 2025
    protocol version. The gateway accepted only 2026-07-28 and answered
    `initialize` with -32601, so none of them could connect to a product whose
    own `agent_type` enum lists `claude_code`.

    The 2026 dialect and its mirrored-header strictness are unchanged; these
    tests cover the second dialect and the boundary between them.
    """

    @pytest.mark.parametrize("requested", ["2025-03-26", "2025-06-18", "2025-11-25"])
    def test_initialize_negotiates_the_requested_version(self, requested: str) -> None:
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_handshake_headers(None),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": requested,
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            },
        )

        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["protocolVersion"] == requested
        assert result["serverInfo"]["name"] == "interlock"
        # Only tools may be advertised: a client that sees resources or prompts
        # will call resources/list, which this server answers with 404.
        assert set(result["capabilities"]) == {"tools"}
        assert "Mcp-Session-Id" not in resp.headers

    def test_initialize_falls_back_to_the_latest_supported_version(self) -> None:
        """The spec says to answer an unknown version with one we do support."""
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_handshake_headers(None),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            },
        )

        assert resp.status_code == 200
        assert resp.json()["result"]["protocolVersion"] == "2025-11-25"

    def test_initialize_requires_a_string_protocol_version(self) -> None:
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_handshake_headers(None),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32602

    def test_initialized_notification_is_accepted(self) -> None:
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_handshake_headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        assert resp.status_code == 202
        assert resp.content == b""

    def test_ping_returns_exactly_an_empty_result(self) -> None:
        """The deployed TypeScript SDK validates empty results strictly.

        Wrapping this in the modern envelope would add `resultType` and
        `_meta` and the client would reject the response.
        """
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_handshake_headers(),
            json={"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        )

        assert resp.status_code == 200
        assert resp.json()["result"] == {}

    def test_tools_list_works_without_any_mirrored_headers(self) -> None:
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_handshake_headers("2025-06-18"),
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )

        assert resp.status_code == 200
        assert resp.json()["result"]["tools"] == MCP_TOOLS

    def test_tools_call_reaches_governance_with_a_bearer_token(self) -> None:
        """The handshake dialect must not bypass authentication."""
        adapter = MCPAdapter()
        app = _make_app(adapter)
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=_identity_with_source_roles([]))
        app.state.auth_manager = auth
        client = TestClient(app)

        resp = client.post(
            "/mcp",
            headers={**_handshake_headers("2025-03-26"), "Authorization": "Bearer key-123"},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "interlock_list_sources", "arguments": {}},
            },
        )

        assert resp.status_code == 200
        auth.authenticate.assert_awaited_once_with("key-123")

    def test_sse_preference_is_honoured_in_the_handshake_dialect(self) -> None:
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_handshake_headers(sse=True),
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

    def test_an_unknown_method_is_still_method_not_found(self) -> None:
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers=_handshake_headers(),
            json={"jsonrpc": "2.0", "id": 6, "method": "resources/list", "params": {}},
        )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == -32601

    def test_the_modern_dialect_still_requires_its_mirrored_headers(self) -> None:
        """The boundary: relaxing 2025 must not relax 2026."""
        client = TestClient(_make_app(MCPAdapter()))

        resp = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
            json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020


class TestSourceUnavailableIsReadable:
    """An agent must learn why a source cannot be used, not only that a tool failed.

    `get_pool` refuses with fixed, safe text - circuit open, disabled, TLS
    refused, unknown - but `_execute_query` and `_describe_source` swallowed
    it into "Error: tool execution failed", so the agent saw an opaque
    `-32602` while the reason sat in the gateway log.
    """

    @staticmethod
    @contextmanager
    def _client_for(get_pool_error: Exception):
        adapter = MCPAdapter(pg_pool=_mock_pool())
        app = _make_app(adapter)
        identity = _identity_with_source_roles([])
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=identity)
        app.state.auth_manager = auth
        conn_manager = MagicMock()
        conn_manager.get_pool = AsyncMock(side_effect=get_pool_error)
        app.state.conn_manager = conn_manager
        pipeline = MagicMock()
        pipeline.preflight = AsyncMock(
            return_value=GatewayDecision(allowed=True, identity=identity)
        )
        pipeline.audit = AsyncMock()
        with patch.object(mcp_adapter.GatewayPipeline, "from_state", return_value=pipeline):
            yield TestClient(app)

    @staticmethod
    def _handshake_call(client, tool: str) -> Any:
        return client.post(
            "/mcp",
            headers={**_handshake_headers(), "Authorization": "Bearer k"},
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": tool, "arguments": _source_args(tool)},
            },
        )

    @pytest.mark.parametrize("tool", ["interlock_query", "interlock_describe_source"])
    def test_an_unavailable_source_is_a_readable_tool_error(self, tool: str) -> None:
        error = DataSourceUnavailableError("Circuit open for data source: src1")
        with self._client_for(error) as client:
            resp = self._handshake_call(client, tool)

        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["isError"] is True
        assert "Circuit open for data source: src1" in result["content"][0]["text"]
        assert result["structuredContent"]["status"] == "unavailable"
        assert result["structuredContent"]["code"] == 503

    def test_an_unknown_source_is_a_readable_not_found(self) -> None:
        error = DataSourceNotFoundError("Unknown data source: src1")
        with self._client_for(error) as client:
            resp = self._handshake_call(client, "interlock_query")

        result = resp.json()["result"]
        assert result["isError"] is True
        assert "Unknown data source: src1" in result["content"][0]["text"]
        assert result["structuredContent"]["status"] == "not_found"
        assert result["structuredContent"]["code"] == 404

    def test_the_modern_dialect_reports_the_reason_in_its_jsonrpc_error(self) -> None:
        error = DataSourceUnavailableError("Data source is disabled: src1")
        with self._client_for(error) as client:
            resp = client.post(
                "/mcp",
                headers={
                    **_streamable_headers("tools/call", name="interlock_query"),
                    "Authorization": "Bearer k",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": _streamable_params(
                        name="interlock_query", arguments=_source_args("interlock_query")
                    ),
                },
            )

        assert resp.status_code == 503
        body = resp.json()["error"]
        assert body["code"] == -32603
        assert "Data source is disabled: src1" in body["message"]

    @pytest.mark.parametrize(
        ("error", "status_code"),
        [
            (DataSourceUnavailableError("Circuit open for data source: src1"), 503),
            (DataSourceNotFoundError("Unknown data source: src1"), 404),
        ],
    )
    def test_the_legacy_route_returns_the_reason_with_its_status(
        self, error: Exception, status_code: int
    ) -> None:
        with self._client_for(error) as client:
            resp = client.post(
                "/mcp/tools/call",
                headers={"Authorization": "Bearer k"},
                json={"name": "interlock_query", "arguments": _source_args("interlock_query")},
            )

        assert resp.status_code == status_code
        assert str(error) in resp.json()["content"][0]["text"]

    def test_any_other_failure_stays_opaque(self) -> None:
        """A raw upstream exception can carry an address; errors-v1 forbids returning it."""
        error = OSError("connect call failed ('10.0.0.5', 5432)")
        with self._client_for(error) as client:
            resp = self._handshake_call(client, "interlock_query")

        assert "10.0.0.5" not in resp.text
        assert resp.status_code == 400

    def test_an_unknown_source_is_still_a_value_error_for_existing_callers(self) -> None:
        assert issubclass(DataSourceNotFoundError, ValueError)


class TestQueryErrorsAreReadable:
    """A statement the database rejects comes back with the database's reason.

    The rc.13 independent evaluation wrote a join on a column that did not
    exist and was told only "tool execution failed", while the audit event
    said `column o.total does not exist`. Query-shape errors now return their
    SQLSTATE and message, with a correlation ID matching the audit row.
    Connection and server errors stay opaque.
    """

    @staticmethod
    @contextmanager
    def _client_for(query_error: Exception):
        adapter = MCPAdapter(pg_pool=_mock_pool())
        app = _make_app(adapter)
        identity = _identity_with_source_roles([])
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=identity)
        app.state.auth_manager = auth
        source_pool = MagicMock()
        source_pool.fetch = AsyncMock(side_effect=query_error)
        conn_manager = MagicMock()
        conn_manager.get_pool = AsyncMock(return_value=source_pool)
        app.state.conn_manager = conn_manager
        pipeline = MagicMock()
        pipeline.preflight = AsyncMock(
            return_value=GatewayDecision(allowed=True, identity=identity)
        )
        pipeline.audit = AsyncMock()
        app.state.audit_logger = AsyncMock()
        adapter._safe_audit = AsyncMock()  # type: ignore[method-assign]
        with patch.object(mcp_adapter.GatewayPipeline, "from_state", return_value=pipeline):
            yield TestClient(app), adapter

    @staticmethod
    def _undefined_column() -> Exception:
        import asyncpg

        return asyncpg.exceptions.UndefinedColumnError("column o.total does not exist")

    def test_the_handshake_dialect_gets_a_readable_query_error(self) -> None:
        with self._client_for(self._undefined_column()) as (client, adapter):
            resp = client.post(
                "/mcp",
                headers={**_handshake_headers(), "Authorization": "Bearer k"},
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "interlock_query",
                        "arguments": {"source_id": "src1", "sql": "SELECT o.total FROM orders o"},
                    },
                },
            )

        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "42703" in text and "column o.total does not exist" in text
        assert result["structuredContent"]["status"] == "query_error"
        # The same correlation ID is in the answer and in the audit row.
        audited = adapter._safe_audit.await_args.kwargs["correlation_id"]
        assert audited and audited in text

    def test_the_modern_dialect_reports_the_query_error_in_its_jsonrpc_error(self) -> None:
        with self._client_for(self._undefined_column()) as (client, _):
            resp = client.post(
                "/mcp",
                headers={
                    **_streamable_headers("tools/call", name="interlock_query"),
                    "Authorization": "Bearer k",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": _streamable_params(
                        name="interlock_query", arguments=_source_args("interlock_query")
                    ),
                },
            )

        body = resp.json()["error"]
        assert body["code"] == -32602
        assert "column o.total does not exist" in body["message"]

    def test_the_legacy_route_returns_a_structured_query_error(self) -> None:
        with self._client_for(self._undefined_column()) as (client, _):
            resp = client.post(
                "/mcp/tools/call",
                headers={"Authorization": "Bearer k", "X-Correlation-ID": "corr-eval-24"},
                json={"name": "interlock_query", "arguments": _source_args("interlock_query")},
            )

        assert resp.status_code == 422
        detail = resp.json()["query_error"]
        assert detail == {
            "category": "query_error",
            "code": "42703",
            "message": "column o.total does not exist",
            "correlation_id": "corr-eval-24",
        }

    def test_a_wrapped_mysql_error_is_classified(self) -> None:
        class OperationalError(Exception):
            pass

        try:
            try:
                raise OperationalError(1054, "Unknown column 'o.total' in 'field list'")
            except OperationalError as inner:
                raise RuntimeError("MySQL query failed") from inner
        except RuntimeError as wrapped:
            error = wrapped
        with self._client_for(error) as (client, _):
            resp = client.post(
                "/mcp/tools/call",
                headers={"Authorization": "Bearer k"},
                json={"name": "interlock_query", "arguments": _source_args("interlock_query")},
            )

        assert resp.status_code == 422
        assert resp.json()["query_error"]["code"] == "MySQL 1054"

    @pytest.mark.parametrize(
        "error",
        [
            OSError("connect call failed ('10.0.0.5', 5432)"),
            pytest.param("connection-failure", id="sqlstate-08"),
            pytest.param("mysql-2003", id="mysql-cannot-connect"),
        ],
    )
    def test_connection_and_server_errors_stay_opaque(self, error: Any) -> None:
        import asyncpg

        if error == "connection-failure":
            error = asyncpg.exceptions.ConnectionFailureError("could not reach 10.0.0.5:5432")
        elif error == "mysql-2003":
            error = Exception(2003, "Can't connect to MySQL server on '10.0.0.5'")
        with self._client_for(error) as (client, _):
            resp = client.post(
                "/mcp/tools/call",
                headers={"Authorization": "Bearer k"},
                json={"name": "interlock_query", "arguments": _source_args("interlock_query")},
            )

        assert resp.status_code == 400
        assert "10.0.0.5" not in resp.text
        assert "tool execution failed" in resp.text
        assert "query_error" not in resp.json()


def _source_args(tool: str) -> dict[str, Any]:
    if tool == "interlock_query":
        return {"source_id": "src1", "sql": "SELECT 1"}
    return {"source_id": "src1"}


class TestEveryAuditRowCarriesTheCorrelationId:
    """The client's X-Correlation-ID reaches every MCP audit row, not only failures.

    Found by `make smoke-eval`: served, cached and denied MCP calls were audited
    under a fresh random ID, so an operator could not find a client's request
    in the audit log by the ID the client sent.
    """

    @staticmethod
    @contextmanager
    def _client(decision_allowed: bool):
        adapter = MCPAdapter(pg_pool=_mock_pool())
        app = _make_app(adapter)
        identity = _identity_with_source_roles([])
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=identity)
        app.state.auth_manager = auth
        source_pool = MagicMock()
        source_pool.fetch = AsyncMock(return_value=[{"id": 1}])
        conn_manager = MagicMock()
        conn_manager.get_pool = AsyncMock(return_value=source_pool)
        app.state.conn_manager = conn_manager
        app.state.audit_logger = AsyncMock()
        pipeline = MagicMock()
        pipeline.preflight = AsyncMock(
            return_value=(
                GatewayDecision(allowed=True, identity=identity)
                if decision_allowed
                else GatewayDecision(
                    allowed=False, status_code=403, reason="Policy denied: test", identity=identity
                )
            )
        )
        pipeline.audit = AsyncMock()
        pipeline_class = MagicMock(return_value=pipeline)
        pipeline_class.from_state = MagicMock(return_value=pipeline)
        with patch.object(mcp_adapter, "GatewayPipeline", pipeline_class):
            yield TestClient(app), pipeline

    @staticmethod
    def _audited_correlation_ids(pipeline: MagicMock) -> list[object]:
        return [
            call.args[0].metadata.get("correlation_id") for call in pipeline.audit.await_args_list
        ]

    def test_a_served_query_is_audited_under_the_clients_id(self) -> None:
        with self._client(decision_allowed=True) as (client, pipeline):
            resp = client.post(
                "/mcp/tools/call",
                headers={"Authorization": "Bearer k", "X-Correlation-ID": "corr-served-1"},
                json={"name": "interlock_query", "arguments": _source_args("interlock_query")},
            )

        assert resp.status_code == 200
        ids = self._audited_correlation_ids(pipeline)
        assert ids and all(i == "corr-served-1" for i in ids)

    def test_a_denied_call_is_audited_under_the_clients_id(self) -> None:
        with self._client(decision_allowed=False) as (client, pipeline):
            resp = client.post(
                "/mcp/tools/call",
                headers={"Authorization": "Bearer k", "X-Correlation-ID": "corr-denied-1"},
                json={"name": "interlock_query", "arguments": _source_args("interlock_query")},
            )

        assert resp.status_code == 403
        assert self._audited_correlation_ids(pipeline) == ["corr-denied-1"]

    def test_without_a_header_one_id_is_chosen_per_call(self) -> None:
        with self._client(decision_allowed=True) as (client, pipeline):
            client.post(
                "/mcp/tools/call",
                headers={"Authorization": "Bearer k"},
                json={"name": "interlock_query", "arguments": _source_args("interlock_query")},
            )

        ids = self._audited_correlation_ids(pipeline)
        assert ids and ids[0] and len(set(ids)) == 1


class TestQueuedWriteAndDenialResults:
    """A governed refusal must reach the agent as something it can read.

    On the canonical endpoint a 202 (queued for approval) flowed through
    `_execute_streamable_tool` as a JSON-RPC *success* whose payload was
    `{"error": ...}` with no `content` array - a malformed tool result. And a
    denial arrived as a 4xx JSON-RPC error, which the TypeScript SDK surfaces
    as "Error POSTing to endpoint", burying the reason.
    """

    @staticmethod
    @contextmanager
    def _client_for(decision: GatewayDecision):
        """A client whose pipeline returns one fixed decision.

        `call_tool` builds its pipeline with `GatewayPipeline.from_state`, so
        the substitution has to happen there rather than by setting an
        attribute on app.state.
        """
        adapter = MCPAdapter()
        app = _make_app(adapter)
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=_identity_with_source_roles([]))
        app.state.auth_manager = auth
        pipeline = MagicMock()
        pipeline.preflight = AsyncMock(return_value=decision)
        pipeline.audit = AsyncMock()
        with patch.object(mcp_adapter.GatewayPipeline, "from_state", return_value=pipeline):
            yield TestClient(app)

    @staticmethod
    def _queued() -> GatewayDecision:
        return GatewayDecision(
            allowed=False,
            reason="Write queued for approval, ID: 42",
            status_code=202,
            approval_id=42,
            approval_state="pending",
        )

    @pytest.mark.parametrize("dialect", ["modern", "handshake"])
    def test_a_queued_write_is_a_well_formed_tool_result(self, dialect: str) -> None:
        args = {"source_id": "src1", "sql": "DELETE FROM customers WHERE id = 2"}
        if dialect == "modern":
            headers = {
                **_streamable_headers("tools/call", name="interlock_query"),
                "Authorization": "Bearer k",
            }
            params = _streamable_params(name="interlock_query", arguments=args)
        else:
            headers = {**_handshake_headers(), "Authorization": "Bearer k"}
            params = {"name": "interlock_query", "arguments": args}

        with self._client_for(self._queued()) as client:
            resp = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
            )

        assert resp.status_code == 200
        result = resp.json()["result"]
        assert "error" not in result
        # `isError` stays false deliberately: Claude Code retries failed tool
        # calls and ApprovalQueue.submit has no dedupe, so a retry would create
        # a second pending approval for one intended write.
        assert result["isError"] is False
        assert result["structuredContent"] == {
            "status": "queued",
            "approval_id": 42,
            "approval_state": "pending",
            "message": "Write queued for approval, ID: 42",
        }
        text = result["content"][0]["text"]
        assert "42" in text
        assert "not executed" in text.lower()
        assert "retry" in text.lower()

    @pytest.mark.parametrize(
        ("status_code", "expected_status"),
        [(403, "denied"), (429, "rate_limited"), (401, "unauthenticated")],
    )
    def test_a_governance_refusal_is_a_readable_tool_error(
        self, status_code: int, expected_status: str
    ) -> None:
        decision = GatewayDecision(
            allowed=False,
            reason="Source role denied: db.table.select on sales.payroll",
            status_code=status_code,
        )

        with self._client_for(decision) as client:
            resp = client.post(
                "/mcp",
                headers={**_handshake_headers(), "Authorization": "Bearer k"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "interlock_query",
                        "arguments": {"source_id": "src1", "sql": "SELECT 1"},
                    },
                },
            )

        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["isError"] is True
        assert "Source role denied" in result["content"][0]["text"]
        assert result["structuredContent"]["status"] == expected_status
        assert result["structuredContent"]["code"] == status_code

    def test_the_modern_dialect_keeps_reporting_refusals_as_jsonrpc_errors(self) -> None:
        """2026-07-28 clients contracted for JSON-RPC errors; that is unchanged."""
        decision = GatewayDecision(allowed=False, reason="Policy denied", status_code=403)

        with self._client_for(decision) as client:
            resp = client.post(
                "/mcp",
                headers={
                    **_streamable_headers("tools/call", name="interlock_query"),
                    "Authorization": "Bearer k",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": _streamable_params(
                        name="interlock_query",
                        arguments={"source_id": "src1", "sql": "SELECT 1"},
                    ),
                },
            )

        assert resp.status_code == 403
        assert "error" in resp.json()

    def test_the_legacy_route_reports_the_approval_id_as_structured_fields(self) -> None:
        """Parity with the HTTP proxy, which has always returned these."""
        with self._client_for(self._queued()) as client:
            resp = client.post(
                "/mcp/tools/call",
                headers={"Authorization": "Bearer k"},
                json={
                    "name": "interlock_query",
                    "arguments": {"source_id": "src1", "sql": "DELETE FROM t WHERE id = 1"},
                },
            )

        assert resp.status_code == 202
        assert resp.json() == {
            "error": "Write queued for approval, ID: 42",
            "approval_id": 42,
            "approval_state": "pending",
        }


# ---------------------------------------------------------------------------
# Redaction evidence
# ---------------------------------------------------------------------------


class _FakeScanner:
    """Redacts a fixed column and reports matches the way the real one does.

    The real scanner returns `PIIMatch` models, not dicts. That distinction is
    the whole point of these tests: the adapter used to filter detections with
    `isinstance(d, dict)`, so nothing ever matched and `pii_types` was always
    empty even though `pii_detected` was set.
    """

    def __init__(self, entity_type: str = "US_SSN") -> None:
        self.entity_type = entity_type

    async def process_row(self, row):
        from interlock.models import PIIMatch

        if "ssn" not in row:
            return dict(row), []
        out = dict(row)
        raw = str(out["ssn"])
        out["ssn"] = f"[REDACTED:{self.entity_type}]"
        return out, [PIIMatch(entity_type=self.entity_type, start=0, end=len(raw), text=raw)]


def _request_with_scanner(scanner):
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pii_scanner=scanner)))


@pytest.mark.asyncio
async def test_redaction_reports_the_entity_types_it_masked() -> None:
    """`pii_types` must name what was found, not come back empty."""
    adapter = MCPAdapter()
    rows = [{"name": "Dana", "ssn": "900-55-0199"}, {"name": "Ada", "ssn": "900-55-0200"}]

    redacted, detected, types, _stats = await adapter._redact_rows(
        _request_with_scanner(_FakeScanner()), rows
    )

    assert detected is True
    assert types == ["US_SSN"]
    assert all(row["ssn"] == "[REDACTED:US_SSN]" for row in redacted)


@pytest.mark.asyncio
async def test_redaction_records_evidence_of_what_it_masked() -> None:
    """The audit needs countable proof, not just a boolean.

    `audit_log.redaction_stats` is what the operator guide points an auditor
    at. It was written only by the HTTP `redact_columns` path, so on the
    primary agent path it was always null and the documented query returned
    nothing.
    """
    adapter = MCPAdapter()
    rows = [{"ssn": "900-55-0199"}, {"ssn": "900-55-0200"}, {"name": "no pii here"}]

    _redacted, _detected, _types, stats = await adapter._redact_rows(
        _request_with_scanner(_FakeScanner()), rows
    )

    assert stats["rows_scanned"] == 3
    assert stats["rows_redacted"] == 2
    assert stats["pii_redactions"] == {"US_SSN": 2}
    assert stats["scanner_failures"] == 0


@pytest.mark.asyncio
async def test_a_clean_payload_records_no_redaction_evidence() -> None:
    """An empty dict, not a misleading zero-filled one."""
    adapter = MCPAdapter()

    _rows, detected, types, stats = await adapter._redact_rows(
        _request_with_scanner(_FakeScanner()), [{"name": "nothing sensitive"}]
    )

    assert detected is False
    assert types == []
    assert stats == {}


@pytest.mark.asyncio
async def test_a_scanner_failure_is_counted_as_well_as_redacted() -> None:
    class _Exploding:
        async def process_row(self, row):
            raise RuntimeError("scanner down")

    adapter = MCPAdapter()

    rows, detected, types, stats = await adapter._redact_rows(
        _request_with_scanner(_Exploding()), [{"ssn": "900-55-0199"}]
    )

    assert detected is True
    assert types == ["PII_SCAN_FAILED"]
    assert stats["scanner_failures"] == 1
    assert rows == [{"error": "row redacted after scanner failure"}]
