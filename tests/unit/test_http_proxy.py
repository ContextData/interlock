"""Unit tests for the HTTP/REST reverse proxy."""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from interlock.connections.circuit_breaker import CircuitBreakerRegistry
from interlock.errors import AuditUnavailableError, WriteApprovalRequiredError
from interlock.gateway.app import _audit_unavailable_handler
from interlock.gateway.http_proxy import (
    HTTPProxy,
    _HTTPIngressError,
    _iter_bounded_redacted_response,
)
from interlock.models import (
    AgentType,
    DataSourceConfig,
    DataSourceType,
    IdentityContext,
    PolicyDecision,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_source(
    source_id: str = "api1",
    base_url: str | None = "https://upstream.example.com/v1",
) -> DataSourceConfig:
    config: dict = {}
    if base_url is not None:
        config["base_url"] = base_url
    return DataSourceConfig(
        source_id=source_id,
        name=f"Test {source_id}",
        source_type=DataSourceType.HTTP,
        connection_config=config,
    )


class FakeRegistry:
    """Minimal registry stub that returns pre-configured sources."""

    def __init__(self, sources: dict[str, DataSourceConfig] | None = None):
        self._sources = sources or {}

    def get(self, source_id: str) -> DataSourceConfig | None:
        return self._sources.get(source_id)


class TrackingByteStream(httpx.AsyncByteStream):
    """Chunked upstream body that records deterministic cleanup."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False
        self.chunks_read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _build_app(
    registry: FakeRegistry | None = None,
    pii_scanner: object | None = None,
    auth_manager: object | None = None,
    approval_queue: object | None = None,
    policy_engine: object | None = None,
    audit_logger: object | None = None,
    circuit_breakers: object | None = None,
    cache_invalidator: object | None = None,
    cache_strategy: object | None = None,
    proxy: HTTPProxy | None = None,
) -> Starlette:
    """Build a minimal Starlette app with the HTTP proxy routes."""
    proxy = proxy or HTTPProxy()

    app = Starlette(routes=proxy.get_routes())
    app.state.registry = registry
    app.state.pii_scanner = pii_scanner
    if auth_manager is not None:
        app.state.auth_manager = auth_manager
    if approval_queue is not None:
        app.state.approval_queue = approval_queue
    if policy_engine is not None:
        app.state.policy_engine = policy_engine
    if audit_logger is not None:
        app.state.audit_logger = audit_logger
    if circuit_breakers is not None:
        app.state.circuit_breakers = circuit_breakers
    if cache_invalidator is not None:
        app.state.cache_invalidator = cache_invalidator
    if cache_strategy is not None:
        app.state.cache_strategy = cache_strategy
    # Store proxy so we can manage its lifecycle
    app.state._proxy = proxy
    return app


def _identity() -> IdentityContext:
    return IdentityContext(
        identity_id=1,
        user="agent",
        agent_type=AgentType.CUSTOM,
        team="eng",
        roles=["writer"],
        session_id="sess-1",
    )


@pytest.fixture(autouse=True)
def _safe_test_dns(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "upstream.example.com":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", port or 443),
                )
            ]
        return []

    monkeypatch.setattr("interlock.security.egress.socket.getaddrinfo", fake_getaddrinfo)


@pytest.fixture()
def source() -> DataSourceConfig:
    return _make_source()


@pytest.fixture()
def registry(source: DataSourceConfig) -> FakeRegistry:
    return FakeRegistry({source.source_id: source})


# ---------------------------------------------------------------------------
# Tests: route registration
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    def test_get_routes_returns_list(self) -> None:
        proxy = HTTPProxy()
        routes = proxy.get_routes()
        assert len(routes) == 1
        assert isinstance(routes[0], Route)

    def test_route_path(self) -> None:
        proxy = HTTPProxy()
        route = proxy.get_routes()[0]
        assert "proxy" in route.path
        assert "source_id" in route.path
        assert "path" in route.path

    def test_route_methods(self) -> None:
        proxy = HTTPProxy()
        route = proxy.get_routes()[0]
        expected = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}
        assert expected == route.methods


# ---------------------------------------------------------------------------
# Tests: error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    def test_legacy_string_false_does_not_allow_private_egress(self) -> None:
        source = _make_source(base_url="http://169.254.169.254/latest/meta-data")
        source.connection_config["allow_private_egress"] = "false"
        app = _build_app(registry=FakeRegistry({source.source_id: source}))

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users")

        assert resp.status_code == 403
        assert resp.json() == {"error": "egress_blocked"}

    def test_strict_audit_failure_returns_canonical_503(self, registry: FakeRegistry) -> None:
        audit = MagicMock()
        audit.log = AsyncMock(side_effect=AuditUnavailableError("spool unavailable"))
        app = _build_app(registry=registry, audit_logger=audit)
        app.add_exception_handler(AuditUnavailableError, _audit_unavailable_handler)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users")

        assert resp.status_code == 503
        assert resp.json() == {"error": "audit_unavailable"}
        assert resp.headers["retry-after"] == "1"

    def test_no_registry_returns_503(self) -> None:
        app = _build_app(registry=None)
        # Explicitly set registry to None (it defaults from _build_app)
        app.state.registry = None
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users")
        assert resp.status_code == 503
        assert "Registry not available" in resp.json()["error"]

    def test_unknown_source_returns_404(self, registry: FakeRegistry) -> None:
        app = _build_app(registry=registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/nonexistent/data")
        assert resp.status_code == 404
        assert "Unknown source" in resp.json()["error"]

    def test_unknown_source_is_audited(self, registry: FakeRegistry) -> None:
        audit = MagicMock()
        audit.log = AsyncMock()
        app = _build_app(registry=registry, audit_logger=audit)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/nonexistent/data")

        assert resp.status_code == 404
        audit.log.assert_awaited_once()
        record = audit.log.await_args.args[0]
        assert record.source_id == "nonexistent"
        assert record.status == "error"
        assert record.error_message == "Unknown source: nonexistent"
        assert record.request_metadata["error_stage"] == "source_lookup"

    def test_source_without_base_url_returns_400(self) -> None:
        no_url_source = _make_source(source_id="nourl", base_url=None)
        reg = FakeRegistry({"nourl": no_url_source})
        app = _build_app(registry=reg)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/nourl/anything")
        assert resp.status_code == 400
        assert "no base_url" in resp.json()["error"]

    @respx.mock
    def test_circuit_breaker_blocks_open_source(self, registry: FakeRegistry) -> None:
        upstream = respx.get("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(200, text="ok")
        )
        circuits = CircuitBreakerRegistry()
        circuits.get("api1").record_pool_exhaustion()
        audit = MagicMock()
        audit.log = AsyncMock()

        app = _build_app(registry=registry, audit_logger=audit, circuit_breakers=circuits)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users")

        assert resp.status_code == 503
        assert upstream.called is False
        audit.log.assert_awaited_once()
        record = audit.log.await_args.args[0]
        assert record.status == "error"
        assert record.request_metadata["error_stage"] == "circuit_breaker"

    @respx.mock
    def test_upstream_500_records_circuit_failure(self, registry: FakeRegistry) -> None:
        respx.get("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(500, text="broken")
        )
        circuits = CircuitBreakerRegistry()

        app = _build_app(registry=registry, circuit_breakers=circuits)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users")

        assert resp.status_code == 500
        breaker = circuits.get("api1")
        assert list(breaker._window)[-1][1] is False


# ---------------------------------------------------------------------------
# Tests: proxy forwarding
# ---------------------------------------------------------------------------


class TestProxyForwarding:
    @respx.mock
    def test_get_forwarded_to_correct_url(self, registry: FakeRegistry) -> None:
        upstream = respx.get("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(200, text="ok")
        )

        app = _build_app(registry=registry)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users")

        assert resp.status_code == 200
        assert upstream.called

    @respx.mock
    def test_get_with_query_params(self, registry: FakeRegistry) -> None:
        upstream = respx.get("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(200, text="filtered")
        )

        app = _build_app(registry=registry)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users?role=admin&limit=10")

        assert resp.status_code == 200
        assert upstream.called
        # Verify query params were forwarded
        call = upstream.calls[0]
        assert "role" in str(call.request.url)
        assert "limit" in str(call.request.url)

    @respx.mock
    def test_post_forwarded_with_body(self, registry: FakeRegistry) -> None:
        upstream = respx.post("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(201, json={"id": 1})
        )

        app = _build_app(registry=registry)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        payload = {"name": "Alice", "email": "alice@example.com"}
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/proxy/api1/users",
                json=payload,
            )

        assert resp.status_code == 201
        assert upstream.called
        sent_body = json.loads(upstream.calls[0].request.content)
        assert sent_body["name"] == "Alice"

    @respx.mock
    def test_successful_write_invalidates_source_cache(self, registry: FakeRegistry) -> None:
        respx.post("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(201, json={"id": 1})
        )
        invalidator = AsyncMock()

        app = _build_app(registry=registry, cache_invalidator=invalidator)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/proxy/api1/users", json={"name": "Alice"})

        assert resp.status_code == 201
        invalidator.invalidate_for_source.assert_awaited_once_with("api1")

    @respx.mock
    def test_get_cache_write_records_source_dependency(self, registry: FakeRegistry) -> None:
        respx.get("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        cache_strategy = AsyncMock()
        cache_result = MagicMock()
        cache_result.hit = False
        cache_result.data = None
        cache_result.tier = None
        cache_strategy.get.return_value = cache_result
        invalidator = AsyncMock()

        app = _build_app(
            registry=registry,
            cache_strategy=cache_strategy,
            cache_invalidator=invalidator,
        )
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users")

        assert resp.status_code == 200
        cache_strategy.put.assert_awaited_once()
        invalidator.record_dependency.assert_awaited_once()
        assert invalidator.record_dependency.await_args.kwargs["source_id"] == "api1"
        assert invalidator.record_dependency.await_args.kwargs["asset_path"] == "users"

    @respx.mock
    def test_success_audit_includes_policy_decision(self, registry: FakeRegistry) -> None:
        respx.get("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=_identity())
        policy = MagicMock()
        policy.evaluate.return_value = PolicyDecision(
            allowed=True,
            reason="matched test policy",
            rule_id=77,
        )
        audit = MagicMock()
        audit.log = AsyncMock()

        app = _build_app(
            registry=registry,
            auth_manager=auth,
            policy_engine=policy,
            audit_logger=audit,
        )
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/users", headers={"Authorization": "Bearer valid"})

        assert resp.status_code == 200
        audit.log.assert_awaited_once()
        record = audit.log.await_args.args[0]
        assert record.policy_decision == {
            "allowed": True,
            "reason": "matched test policy",
            "rule_id": 77,
            "rule_name": None,
            "redact_columns": [],
            "rate_limit": None,
            "write_risk_cap": None,
        }

    @respx.mock
    def test_upstream_status_preserved(self, registry: FakeRegistry) -> None:
        respx.delete("https://upstream.example.com/v1/users/42").mock(
            return_value=httpx.Response(204)
        )

        app = _build_app(registry=registry)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.delete("/proxy/api1/users/42")

        assert resp.status_code == 204

    @respx.mock
    def test_delete_queues_approval_before_upstream(
        self,
        registry: FakeRegistry,
    ) -> None:
        upstream = respx.delete("https://upstream.example.com/v1/users/42").mock(
            return_value=httpx.Response(204)
        )
        auth = MagicMock()
        auth.authenticate = AsyncMock(return_value=_identity())
        queue = MagicMock()
        queue.submit = AsyncMock(side_effect=WriteApprovalRequiredError(123, "high"))

        app = _build_app(
            registry=registry,
            auth_manager=auth,
            approval_queue=queue,
        )
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.delete(
                "/proxy/api1/users/42",
                headers={
                    "Authorization": "Bearer valid",
                    "Content-Type": "application/json",
                },
            )

        assert resp.status_code == 202
        assert resp.json()["approval_id"] == 123
        assert upstream.called is False
        queue.submit.assert_awaited_once()
        metadata = queue.submit.await_args.kwargs["metadata"]
        assert metadata["protocol"] == "http"
        assert metadata["method"] == "DELETE"
        assert metadata["path"] == "users/42"
        assert metadata["headers"]["content-type"] == "application/json"


# ---------------------------------------------------------------------------
# Tests: ingress hardening
# ---------------------------------------------------------------------------


class TestIngressHardening:
    @respx.mock
    def test_request_body_over_limit_returns_413_before_upstream(
        self,
        registry: FakeRegistry,
    ) -> None:
        upstream = respx.post("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(201, json={"id": 1})
        )
        app = _build_app(
            registry=registry,
            proxy=HTTPProxy(max_request_bytes=8),
        )
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/proxy/api1/users", content=b"x" * 9)

        assert resp.status_code == 413
        assert resp.json() == {"error": "request_too_large"}
        assert upstream.called is False

    @respx.mock
    def test_unsupported_request_content_encoding_returns_422(
        self,
        registry: FakeRegistry,
    ) -> None:
        upstream = respx.post("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(201, json={"id": 1})
        )
        app = _build_app(registry=registry)
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/proxy/api1/users",
                content=b"compressed",
                headers={"Content-Encoding": "gzip"},
            )

        assert resp.status_code == 422
        assert resp.json() == {"error": "unsupported_content_encoding"}
        assert upstream.called is False

    @respx.mock
    def test_path_traversal_returns_400_before_upstream(self, registry: FakeRegistry) -> None:
        upstream = respx.get("https://upstream.example.com/v1/admin").mock(
            return_value=httpx.Response(200, text="admin")
        )
        app = _build_app(registry=registry)
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/%2e%2e/admin")

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid_path"}
        assert upstream.called is False

    @respx.mock
    def test_absolute_url_smuggling_returns_400_before_upstream(
        self,
        registry: FakeRegistry,
    ) -> None:
        upstream = respx.get("https://upstream.example.com/v1/https://evil.example").mock(
            return_value=httpx.Response(200, text="nope")
        )
        app = _build_app(registry=registry)
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/https:%2f%2fevil.example")

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid_path"}
        assert upstream.called is False

    @respx.mock
    def test_forwarded_request_headers_are_explicitly_allowlisted(
        self,
        registry: FakeRegistry,
    ) -> None:
        upstream = respx.post("https://upstream.example.com/v1/users").mock(
            return_value=httpx.Response(201, json={"id": 1})
        )
        app = _build_app(registry=registry)
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/proxy/api1/users",
                content=b"{}",
                headers={
                    "Authorization": "Bearer interlock-key",
                    "Cookie": "session=secret",
                    "X-Forwarded-For": "10.0.0.5",
                    "X-Custom": "drop-me",
                    "If-None-Match": '"abc"',
                    "Content-Type": "application/json",
                },
            )

        assert resp.status_code == 201
        sent = upstream.calls[0].request.headers
        assert sent["if-none-match"] == '"abc"'
        assert sent["content-type"] == "application/json"
        assert "authorization" not in sent
        assert "cookie" not in sent
        assert "x-forwarded-for" not in sent
        assert "x-custom" not in sent
        assert "accept-encoding" not in sent
        assert "user-agent" not in sent

    @respx.mock
    def test_response_headers_strip_cookie_location_and_unallowlisted_headers(
        self,
        registry: FakeRegistry,
    ) -> None:
        respx.get("https://upstream.example.com/v1/redirect").mock(
            return_value=httpx.Response(
                302,
                text="go",
                headers={
                    "content-type": "text/plain",
                    "etag": '"v1"',
                    "location": "http://169.254.169.254/latest",
                    "set-cookie": "sid=secret",
                    "x-powered-by": "upstream",
                },
            )
        )
        app = _build_app(registry=registry)
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/redirect", follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["content-type"].startswith("text/plain")
        assert resp.headers["etag"] == '"v1"'
        assert "location" not in resp.headers
        assert "set-cookie" not in resp.headers
        assert "x-powered-by" not in resp.headers

    @respx.mock
    def test_upstream_response_over_limit_returns_413(self, registry: FakeRegistry) -> None:
        respx.get("https://upstream.example.com/v1/large").mock(
            return_value=httpx.Response(200, content=b"x" * 9)
        )
        app = _build_app(
            registry=registry,
            proxy=HTTPProxy(max_response_bytes=8),
        )
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/large")

        assert resp.status_code == 413
        assert resp.json() == {"error": "response_too_large"}

    @respx.mock
    def test_upstream_transport_errors_are_sanitized(self, registry: FakeRegistry) -> None:
        respx.get("https://upstream.example.com/v1/broken").mock(
            side_effect=httpx.ConnectError("token=secret upstream refused")
        )
        app = _build_app(registry=registry)
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/broken")

        assert resp.status_code == 502
        assert resp.json() == {"error": "upstream_request_failed"}
        assert "secret" not in resp.text


# ---------------------------------------------------------------------------
# Tests: PII scanning
# ---------------------------------------------------------------------------


class TestPIIScanning:
    @respx.mock
    def test_json_response_gets_pii_scanned(self, registry: FakeRegistry) -> None:
        """PII scanner should be invoked for JSON responses."""
        original = {"name": "Bob", "ssn": "123-45-6789"}
        redacted = {"name": "Bob", "ssn": "[REDACTED:SSN]"}

        respx.get("https://upstream.example.com/v1/record").mock(
            return_value=httpx.Response(
                200,
                json=original,
                headers={"content-type": "application/json"},
            )
        )

        scanner = AsyncMock()
        scanner.process_row = AsyncMock(return_value=(redacted, []))

        app = _build_app(registry=registry, pii_scanner=scanner)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/record")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ssn"] == "[REDACTED:SSN]"
        scanner.process_row.assert_called_once()

    @respx.mock
    def test_json_list_response_scanned(self, registry: FakeRegistry) -> None:
        """Each dict item in a JSON list should be scanned individually."""
        items = [
            {"email": "a@b.com"},
            {"email": "c@d.com"},
        ]
        redacted_items = [
            {"email": "[REDACTED:EMAIL]"},
            {"email": "[REDACTED:EMAIL]"},
        ]

        respx.get("https://upstream.example.com/v1/list").mock(
            return_value=httpx.Response(
                200,
                json=items,
                headers={"content-type": "application/json"},
            )
        )

        scanner = AsyncMock()
        scanner.process_row = AsyncMock(
            side_effect=[
                (redacted_items[0], []),
                (redacted_items[1], []),
            ]
        )

        app = _build_app(registry=registry, pii_scanner=scanner)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/list")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["email"] == "[REDACTED:EMAIL]"
        assert scanner.process_row.call_count == 2

    @respx.mock
    def test_non_json_response_passed_through(self, registry: FakeRegistry) -> None:
        """Non-JSON responses should not be scanned."""
        html = b"<html><body>Hello</body></html>"

        respx.get("https://upstream.example.com/v1/page").mock(
            return_value=httpx.Response(
                200,
                content=html,
                headers={"content-type": "text/html"},
            )
        )

        scanner = AsyncMock()

        app = _build_app(registry=registry, pii_scanner=scanner)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/page")

        assert resp.status_code == 200
        assert b"Hello" in resp.content
        scanner.process_row.assert_not_called()

    @respx.mock
    def test_no_scanner_passes_json_through(self, registry: FakeRegistry) -> None:
        """When no PII scanner is set, JSON should pass through unchanged."""
        original = {"ssn": "123-45-6789"}

        respx.get("https://upstream.example.com/v1/data").mock(
            return_value=httpx.Response(
                200,
                json=original,
                headers={"content-type": "application/json"},
            )
        )

        app = _build_app(registry=registry, pii_scanner=None)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/data")

        assert resp.status_code == 200
        assert resp.json()["ssn"] == "123-45-6789"

    @respx.mock
    def test_json_scanner_failure_redacts_response_body(self, registry: FakeRegistry) -> None:
        respx.get("https://upstream.example.com/v1/data").mock(
            return_value=httpx.Response(
                200,
                json={"ssn": "123-45-6789"},
                headers={"content-type": "application/json"},
            )
        )

        scanner = AsyncMock()
        scanner.process_row = AsyncMock(side_effect=RuntimeError("scanner failed"))
        app = _build_app(registry=registry, pii_scanner=scanner)
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/data")

        assert resp.status_code == 200
        assert "123-45-6789" not in resp.text
        assert resp.json() == {"error": "response redacted after scanner failure"}

    @respx.mock
    def test_text_scanner_failure_redacts_response_body(self, registry: FakeRegistry) -> None:
        respx.get("https://upstream.example.com/v1/text").mock(
            return_value=httpx.Response(
                200,
                text="call 415-555-1212",
                headers={"content-type": "text/plain"},
            )
        )

        scanner = MagicMock()
        scanner.redact.side_effect = RuntimeError("scanner failed")
        app = _build_app(registry=registry)
        app.state.pii_fast_scanner = scanner
        proxy = app.state._proxy
        proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/proxy/api1/text")

        assert resp.status_code == 200
        assert resp.text == "[REDACTED:SCAN_FAILED]"


# ---------------------------------------------------------------------------
# Tests: streaming response redaction and backpressure
# ---------------------------------------------------------------------------


class TestStreamingResponses:
    @respx.mock
    def test_text_stream_redacts_pii_split_across_upstream_chunks(
        self,
        registry: FakeRegistry,
    ) -> None:
        stream = TrackingByteStream([b"call 415-555-", b"1212\n", b"safe\n"])
        respx.get("https://upstream.example.com/v1/text-stream").mock(
            return_value=httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/plain"},
            )
        )
        scanner = MagicMock()
        scanner.redact.side_effect = lambda value: value.replace("415-555-1212", "[REDACTED:PHONE]")
        cache_strategy = AsyncMock()
        cache_miss = MagicMock(hit=False, data=None, tier=None)
        cache_strategy.get.return_value = cache_miss
        app = _build_app(
            registry=registry,
            cache_strategy=cache_strategy,
        )
        app.state.pii_fast_scanner = scanner
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/proxy/api1/text-stream")

        assert response.status_code == 200
        assert response.text == "call [REDACTED:PHONE]\nsafe\n"
        assert "415-555-1212" not in response.text
        assert scanner.redact.call_args_list[0].args[0] == "call 415-555-1212\n"
        assert stream.closed is True
        cache_strategy.put.assert_not_awaited()

    @respx.mock
    def test_ndjson_stream_scans_complete_records_across_chunk_boundaries(
        self,
        registry: FakeRegistry,
    ) -> None:
        stream = TrackingByteStream(
            [
                b'{"name":"Ada","ssn":"123-',
                b'45-6789"}\n{"name":"Lin"',
                b"}\n",
            ]
        )
        respx.get("https://upstream.example.com/v1/events").mock(
            return_value=httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "application/x-ndjson"},
            )
        )
        scanner = AsyncMock()
        scanner.process_row = AsyncMock(
            side_effect=[
                ({"name": "Ada", "ssn": "[REDACTED:SSN]"}, []),
                ({"name": "Lin"}, []),
            ]
        )
        app = _build_app(registry=registry, pii_scanner=scanner)
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/proxy/api1/events")

        assert response.status_code == 200
        records = [json.loads(line) for line in response.text.splitlines()]
        assert records == [
            {"name": "Ada", "ssn": "[REDACTED:SSN]"},
            {"name": "Lin"},
        ]
        assert scanner.process_row.await_count == 2
        assert stream.closed is True

    @respx.mock
    def test_stream_scanner_failure_fails_closed_without_cache_write(
        self,
        registry: FakeRegistry,
    ) -> None:
        stream = TrackingByteStream([b"secret 415-555-1212\n"])
        respx.get("https://upstream.example.com/v1/failing-stream").mock(
            return_value=httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/csv"},
            )
        )
        scanner = MagicMock()
        scanner.redact.side_effect = RuntimeError("scanner unavailable")
        cache_strategy = AsyncMock()
        cache_strategy.get.return_value = MagicMock(hit=False, data=None, tier=None)
        app = _build_app(registry=registry, cache_strategy=cache_strategy)
        app.state.pii_fast_scanner = scanner
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/proxy/api1/failing-stream")

        assert response.status_code == 200
        assert response.text == "[REDACTED:SCAN_FAILED]\n"
        assert "415-555-1212" not in response.text
        assert stream.closed is True
        cache_strategy.put.assert_not_awaited()

    @respx.mock
    def test_stream_completion_records_circuit_success_and_final_audit(
        self,
        registry: FakeRegistry,
    ) -> None:
        stream = TrackingByteStream([b"one\n", b"two\n"])
        respx.get("https://upstream.example.com/v1/audited-stream").mock(
            return_value=httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/plain"},
            )
        )
        circuits = CircuitBreakerRegistry()
        audit = MagicMock()
        audit.log = AsyncMock()
        app = _build_app(
            registry=registry,
            audit_logger=audit,
            circuit_breakers=circuits,
        )
        app.state._proxy._client = httpx.AsyncClient()

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/proxy/api1/audited-stream")

        assert response.status_code == 200
        assert response.text == "one\ntwo\n"
        assert list(circuits.get("api1")._window)[-1][1] is True
        audit.log.assert_awaited_once()
        record = audit.log.await_args.args[0]
        assert record.status == "success"
        assert record.request_metadata["redaction_stats"] == {
            "streamed": True,
            "bytes_in": 8,
            "bytes_out": 8,
            "records": 2,
            "scanner_failures": 0,
            "completed": True,
        }

    @pytest.mark.anyio
    async def test_stream_generator_closes_upstream_when_consumer_cancels(self) -> None:
        stream = TrackingByteStream([b"first\n", b"second\n"])
        request = MagicMock()
        request.app.state.pii_fast_scanner = None
        response = httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "text/plain"},
        )
        body = _iter_bounded_redacted_response(
            response,
            request=request,
            content_type="text/plain",
            policy_redactions=[],
            max_bytes=1024,
        )

        assert await anext(body) == b"first\n"
        assert stream.chunks_read == 1
        assert stream.closed is False

        await body.aclose()

        assert stream.closed is True
        assert stream.chunks_read == 1

    @pytest.mark.anyio
    async def test_chunked_stream_enforces_decoded_response_limit(self) -> None:
        stream = TrackingByteStream([b"1234\n", b"5678\n"])
        request = MagicMock()
        request.app.state.pii_fast_scanner = None
        response = httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "text/plain"},
        )
        body = _iter_bounded_redacted_response(
            response,
            request=request,
            content_type="text/plain",
            policy_redactions=[],
            max_bytes=8,
        )

        assert await anext(body) == b"1234\n"
        with pytest.raises(_HTTPIngressError, match="response_too_large"):
            await anext(body)

        assert stream.closed is True
        assert stream.chunks_read == 2


# ---------------------------------------------------------------------------
# Tests: initialize / shutdown lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.anyio
    async def test_initialize_creates_client(self) -> None:
        proxy = HTTPProxy()
        assert proxy._client is None
        await proxy.initialize()
        assert proxy._client is not None
        await proxy.shutdown()

    @pytest.mark.anyio
    async def test_shutdown_closes_client(self) -> None:
        proxy = HTTPProxy()
        await proxy.initialize()
        await proxy.shutdown()
        assert proxy._client is None

    @pytest.mark.anyio
    async def test_shutdown_noop_when_no_client(self) -> None:
        proxy = HTTPProxy()
        # Should not raise
        await proxy.shutdown()
