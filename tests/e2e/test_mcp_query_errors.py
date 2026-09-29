"""A query the database rejects comes back with the database's reason.

The rc.13 independent evaluation wrote a join on columns that did not exist
and got only "tool execution failed" over MCP; the reason was in the audit
event. This drives the same mistake against the real PostgreSQL source and
checks the agent's answer and the audit row agree on the correlation ID.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.e2e.support.clients import agent_headers, mcp_call, wait_for

pytestmark = [pytest.mark.e2e]


@pytest.mark.asyncio
async def test_an_unknown_column_is_reported_with_its_sqlstate(
    e2e_config: Any, control_db: Any
) -> None:
    correlation_id = f"e2e-query-error-{uuid.uuid4().hex[:8]}"
    response = mcp_call(
        e2e_config,
        "interlock_query",
        {
            "source_id": e2e_config.source_id_pg,
            "sql": "SELECT c.no_such_column FROM customers c",
        },
        headers={**agent_headers(e2e_config), "X-Correlation-ID": correlation_id},
    )

    assert response.status_code == 422, response.text
    detail = response.json()["query_error"]
    assert detail["code"] == "42703"
    assert "no_such_column" in detail["message"]
    assert detail["correlation_id"] == correlation_id

    async def audited() -> Any:
        return await control_db.fetchrow(
            "SELECT status, error_message, request_metadata FROM audit_log "
            "WHERE request_metadata->>'correlation_id' = $1",
            correlation_id,
        )

    row = await wait_for(audited)
    assert row is not None, "no audit row carries the correlation ID the agent was given"
    assert row["status"] == "error"
    metadata = row["request_metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["correlation_id"] == correlation_id
