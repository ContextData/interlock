"""Regression test for audit P0-B: MCP query path runs through the
unified governance pipeline.

AUDIT-COVERS: P0-B

The audit reported that ``MCPAdapter._execute_query`` called
``pool.fetch(sql)`` directly: no normalize, no policy check beyond the
top-level allow/deny, no cache, no response redaction, no audit.

Phase 1 brings MCP to parity with the PG simple-query path. The unified
``RequestPipeline`` (P1-T01) is a Phase 2 deliverable; until then the
adapter explicitly invokes the same components: write classifier,
cache strategy with identity-scoped key, response processor for PII,
and audit logger.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from interlock.gateway.mcp_adapter import MCPAdapter
from interlock.models import IdentityContext


def _make_identity() -> IdentityContext:
    return IdentityContext(
        identity_id=1,
        user="alice",
        agent_type="claude_code",
        team="t1",
        roles=["read"],
        mapped_pg_role="onyx_reader",
        session_id="sess-1",
    )


def _make_request_with_state(state: SimpleNamespace) -> Request:
    """Build a minimal ASGI scope that exposes ``request.state`` and ``app.state``."""
    app = SimpleNamespace(state=state)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/tools/call",
        "headers": [],
        "query_string": b"",
        "app": app,
        "state": {},
    }
    req = Request(scope)
    req.state.identity = _make_identity()
    return req


@pytest.mark.asyncio
async def test_p0_b_query_uses_cache_strategy_when_present() -> None:
    cache_strategy = AsyncMock()
    cache_result = MagicMock()
    cache_result.hit = True
    cache_result.data = b'[{"x": 1}]'
    cache_result.tier = "l1"
    cache_strategy.get.return_value = cache_result

    audit = AsyncMock()

    state = SimpleNamespace(
        cache_strategy=cache_strategy,
        audit_logger=audit,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    pool = AsyncMock()
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(request, {"source_id": "test-source", "sql": "SELECT 1"})

    assert resp.status_code == 200
    cache_strategy.get.assert_awaited_once()
    pool.fetch.assert_not_called()
    audit.log.assert_awaited()  # cache hit emits audit


@pytest.mark.asyncio
async def test_p0_b_read_cache_write_records_dependencies() -> None:
    pool = AsyncMock()
    pool.fetch.return_value = [{"id": 1}]

    cache_strategy = AsyncMock()
    cache_result = MagicMock()
    cache_result.hit = False
    cache_result.data = None
    cache_result.tier = None
    cache_strategy.get.return_value = cache_result

    invalidator = AsyncMock()

    state = SimpleNamespace(
        cache_strategy=cache_strategy,
        cache_invalidator=invalidator,
        audit_logger=None,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(
        request,
        {"sql": "SELECT id FROM users", "source_id": "src1"},
    )

    assert resp.status_code == 200
    cache_strategy.put.assert_awaited_once()
    invalidator.record_dependency.assert_awaited_once()
    assert invalidator.record_dependency.await_args.kwargs["source_id"] == "src1"
    assert invalidator.record_dependency.await_args.kwargs["tables"] == ["users"]


@pytest.mark.asyncio
async def test_p0_b_successful_write_invalidates_table_dependencies() -> None:
    pool = AsyncMock()
    pool.fetch.return_value = []
    invalidator = AsyncMock()

    state = SimpleNamespace(
        cache_strategy=None,
        cache_invalidator=invalidator,
        audit_logger=None,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(
        request,
        {"sql": "UPDATE users SET name = 'alice' WHERE id = 1", "source_id": "src1"},
    )

    assert resp.status_code == 200
    invalidator.invalidate_for_tables.assert_awaited_once_with("src1", ["users"])


@pytest.mark.asyncio
async def test_p0_b_select_then_set_search_path_does_not_hit_cache_or_origin() -> None:
    from interlock.core.write_classifier import WriteClassifier

    pool = AsyncMock()
    cache_strategy = AsyncMock()
    state = SimpleNamespace(
        cache_strategy=cache_strategy,
        audit_logger=None,
        write_classifier=WriteClassifier(),
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(
        request,
        {"sql": "SELECT 1; SET search_path TO private", "source_id": "src1"},
    )

    assert resp.status_code == 403
    assert b"requires approval" in resp.body
    cache_strategy.get.assert_not_called()
    pool.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_p0_b_multi_statement_read_is_not_cached_under_first_statement() -> None:
    pool = AsyncMock()
    pool.fetch.return_value = [{"x": 1}]
    cache_strategy = AsyncMock()
    state = SimpleNamespace(
        cache_strategy=cache_strategy,
        audit_logger=None,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(
        request,
        {"sql": "SELECT 1; SELECT 2", "source_id": "src1"},
    )

    assert resp.status_code == 200
    cache_strategy.get.assert_not_called()
    cache_strategy.put.assert_not_called()
    pool.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_p0_b_query_redacts_pii_on_origin_path() -> None:
    pool = AsyncMock()
    pool.fetch.return_value = [{"name": "alice", "ssn": "123-45-6789"}]

    pii_scanner = AsyncMock()

    async def _process_row(row):
        if row.get("ssn"):
            return ({"name": row["name"], "ssn": "***-**-****"}, [{"type": "SSN"}])
        return (row, [])

    pii_scanner.process_row = AsyncMock(side_effect=_process_row)

    audit = AsyncMock()

    state = SimpleNamespace(
        cache_strategy=None,
        audit_logger=audit,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=pii_scanner,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(
        request, {"source_id": "test-source", "sql": "SELECT name, ssn FROM c"}
    )

    body = resp.body.decode()
    assert "123-45-6789" not in body
    assert "***-**-****" in body
    audit.log.assert_awaited()


@pytest.mark.asyncio
async def test_p0_b_query_scanner_failure_redacts_mcp_row() -> None:
    pool = AsyncMock()
    pool.fetch.return_value = [{"name": "alice", "ssn": "123-45-6789"}]

    pii_scanner = AsyncMock()
    pii_scanner.process_row = AsyncMock(side_effect=RuntimeError("scanner failed"))
    audit = AsyncMock()

    state = SimpleNamespace(
        cache_strategy=None,
        audit_logger=audit,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=pii_scanner,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(
        request, {"source_id": "test-source", "sql": "SELECT name, ssn FROM c"}
    )

    body = resp.body.decode()
    assert "123-45-6789" not in body
    assert "row redacted after scanner failure" in body
    audit.log.assert_awaited()


@pytest.mark.asyncio
async def test_p0_b_query_emits_audit_on_origin_fetch() -> None:
    pool = AsyncMock()
    pool.fetch.return_value = []
    audit = AsyncMock()

    state = SimpleNamespace(
        cache_strategy=None,
        audit_logger=audit,
        write_classifier=None,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    await adapter._execute_query(request, {"source_id": "test-source", "sql": "SELECT 1"})

    audit.log.assert_awaited_once()
    record = audit.log.await_args.args[0]
    assert record.identity_id == 1
    assert record.operation == "read"
    assert record.cache_hit is False
    assert record.request_metadata["protocol"] == "mcp"


@pytest.mark.asyncio
async def test_p0_b_high_risk_write_routes_to_approval_queue() -> None:
    from interlock.errors import WriteApprovalRequiredError
    from interlock.models import RiskLevel

    pool = AsyncMock()
    classification = MagicMock()
    classification.blocked = False
    classification.operation_type = "DROP"
    classification.risk_level = RiskLevel.HIGH

    classifier = MagicMock()
    classifier.classify.return_value = classification

    queue = AsyncMock()
    queue.submit.side_effect = WriteApprovalRequiredError(42, "high")

    audit = AsyncMock()

    state = SimpleNamespace(
        cache_strategy=None,
        audit_logger=audit,
        write_classifier=classifier,
        approval_queue=queue,
        pii_scanner=None,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(
        request, {"source_id": "test-source", "sql": "DROP TABLE customers"}
    )

    assert resp.status_code == 202
    pool.fetch.assert_not_called()
    queue.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_p0_b_blocked_write_returns_403() -> None:
    pool = AsyncMock()
    classification = MagicMock()
    classification.blocked = True
    classification.operation_type = "EXECUTE"

    classifier = MagicMock()
    classifier.classify.return_value = classification

    state = SimpleNamespace(
        cache_strategy=None,
        audit_logger=None,
        write_classifier=classifier,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    request = _make_request_with_state(state)

    adapter = MCPAdapter(pg_pool=pool)
    resp = await adapter._execute_query(request, {"source_id": "test-source", "sql": "EXECUTE foo"})

    assert resp.status_code == 403
    pool.fetch.assert_not_called()
