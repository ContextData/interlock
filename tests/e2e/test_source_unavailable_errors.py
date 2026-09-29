"""A source InterLock cannot use must say why, on every protocol.

The gateway knew why - circuit open, source disabled, upstream
TLS refused, upstream unreachable - and logged it, but an MCP agent received
an opaque "tool execution failed" and a PostgreSQL client saw its connection
dropped.

The compose stack is not production mode, so the TLS refusal wording is
proven in unit tests. This proves the mechanism on the real protocols, using a
PostgreSQL source whose upstream port is closed: the PG-wire client must get an
ErrorResponse, and an MCP agent must be told the circuit is open once enough
attempts have failed.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

import asyncpg
import httpx
import pytest

from tests.e2e.support.clients import wait_for

pytestmark = [pytest.mark.e2e]

# Nothing listens here, so every upstream connection is refused at once.
_CLOSED_PORT = 1
_HANDSHAKE_VERSION = "2025-11-25"


def _tool_call(e2e_config: Any, name: str, arguments: dict[str, Any]) -> httpx.Response:
    """A tools/call as a 2025-dialect client sends it - what deployed agents speak."""
    with httpx.Client(base_url=e2e_config.gateway_url, timeout=15) as client:
        return client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {e2e_config.agent_api_key}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": _HANDSHAKE_VERSION,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )


def _tool_result(response: httpx.Response) -> dict[str, Any] | None:
    if response.status_code != 200:
        return None
    result = response.json().get("result")
    return result if isinstance(result, dict) else None


async def _notify(control_db: Any) -> None:
    await control_db.execute("NOTIFY onyx_config_changed")
    await control_db.execute(
        "SELECT pg_notify('onyx_policy_changed', $1)",
        json.dumps({"test": "source_unavailable_errors"}),
    )


@pytest.fixture
async def unreachable_source(e2e_config: Any, control_db: Any) -> Any:
    """A registered, granted, policy-allowed PostgreSQL source nothing listens for."""
    source_id = f"e2e_unreachable_{secrets.token_hex(4)}"
    policy_name = f"e2e-unreachable-allow-{source_id}"
    agent_id = await control_db.fetchval("SELECT id FROM identities WHERE name = 'e2e-agent'")
    assert agent_id, "the seeded e2e agent identity is missing"
    config = {
        "allow_private_egress": True,
        "host": e2e_config.compose_source_pg_host,
        "port": _CLOSED_PORT,
        "database": e2e_config.source_pg_database,
        "user": e2e_config.source_pg_user,
        "password": e2e_config.source_pg_password,
    }
    role_id = None
    try:
        await control_db.execute(
            """
            INSERT INTO data_sources
                (source_id, name, source_type, connection_config, cache_strategy, enabled, metadata)
            VALUES ($1, 'E2E Unreachable PostgreSQL', 'postgresql', $2::jsonb,
                    'bypass', TRUE, '{}'::jsonb)
            """,
            source_id,
            json.dumps(config),
        )
        role_id = await control_db.fetchval(
            """
            INSERT INTO source_roles
                (source_id, role_key, name, description, review_required, metadata)
            VALUES ($1, 'analyst', 'Analyst', 'E2E unreachable source role', FALSE, '{}'::jsonb)
            RETURNING id
            """,
            source_id,
        )
        await control_db.execute(
            "SELECT agentgate_seed_source_role_permissions($1, 'analyst', 'postgresql')",
            role_id,
        )
        await control_db.execute(
            """
            INSERT INTO identity_source_role_grants (identity_id, source_id, role_id, metadata)
            VALUES ($1, $2, $3, '{}'::jsonb)
            """,
            agent_id,
            source_id,
            role_id,
        )
        await control_db.execute(
            """
            INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
            VALUES ($1, 10, $2::jsonb, $3::jsonb, TRUE)
            """,
            policy_name,
            json.dumps(
                {"source_ids": [source_id], "roles": ["analyst"], "operation_types": ["read"]}
            ),
            json.dumps({"effect": "allow"}),
        )
        await _notify(control_db)
        yield source_id
    finally:
        # A leaked enabled source would take traffic from the connector
        # uniformity suite, so it is disabled before anything that could fail.
        await control_db.execute(
            "UPDATE data_sources SET enabled = FALSE WHERE source_id = $1", source_id
        )
        await control_db.execute("DELETE FROM policy_rules WHERE name = $1", policy_name)
        await control_db.execute(
            "DELETE FROM identity_source_role_grants WHERE source_id = $1", source_id
        )
        if role_id is not None:
            await control_db.execute(
                "DELETE FROM source_role_permissions WHERE role_id = $1", role_id
            )
        await control_db.execute("DELETE FROM source_roles WHERE source_id = $1", source_id)
        await control_db.execute("DELETE FROM data_sources WHERE source_id = $1", source_id)
        await _notify(control_db)


async def test_a_pg_wire_client_is_told_the_upstream_connection_failed(
    e2e_config: Any, unreachable_source: str
) -> None:
    params = {**e2e_config.gateway_pg_params_api_key, "database": unreachable_source}

    async def attempt() -> BaseException | None:
        try:
            conn = await asyncpg.connect(**params, timeout=10)
        except Exception as exc:  # noqa: BLE001 - the error is what is under test
            # Until the gateway reloads its registry the source is unknown.
            return None if getattr(exc, "sqlstate", None) == "3D000" else exc
        await conn.close()
        return AssertionError("connected to a source whose upstream port is closed")

    error = await wait_for(attempt, timeout_seconds=20)
    # Before the fix this was a ConnectionDoesNotExistError: no ErrorResponse.
    assert isinstance(error, asyncpg.PostgresError), repr(error)
    assert error.sqlstate == "08006"
    assert "Upstream PostgreSQL connection failed" in str(error)


async def test_an_mcp_agent_is_told_the_circuit_is_open(
    e2e_config: Any, unreachable_source: str
) -> None:
    # The breaker opens after ten failed connections inside its window. Until
    # then each attempt fails on a refused socket, which stays opaque.
    marker = f"Circuit open for data source: {unreachable_source}"
    response = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = _tool_call(
            e2e_config,
            "interlock_query",
            {"source_id": unreachable_source, "sql": "SELECT 1"},
        )
        if marker in response.text:
            break
        await asyncio.sleep(0.1)

    assert response is not None
    result = _tool_result(response)
    assert result is not None, response.text
    assert result["isError"] is True
    assert marker in result["content"][0]["text"]
    assert result["structuredContent"]["status"] == "unavailable"


def test_an_unknown_source_is_a_readable_refusal(e2e_config: Any) -> None:
    source_id = f"e2e_no_such_source_{secrets.token_hex(4)}"
    response = _tool_call(
        e2e_config, "interlock_query", {"source_id": source_id, "sql": "SELECT 1"}
    )

    result = _tool_result(response)
    assert result is not None, response.text
    assert result["isError"] is True
    assert "tool execution failed" not in response.text
    # Preflight answers first: no grant can exist for an unregistered source,
    # so the source-role check refuses it before the connection manager is
    # asked. A 404 from the manager is the answer when preflight lets one by.
    assert result["structuredContent"]["status"] in {"denied", "not_found"}
