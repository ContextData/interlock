"""The same statement over two protocols must never share a cache entry.

An independent evaluation (29 September 2026, rc.13) ran identical SQL through
MCP and then through `psql`. The second `psql` read failed with
`lost synchronization with server: got message type "["`: the PostgreSQL proxy
had found MCP's cached JSON under the same key and written it to the socket as
if it were wire-protocol bytes. The reverse order sent PostgreSQL wire bytes to
an MCP agent as its "result".

The key-level tests in `test_cache_correctness_guarantees.py` could not catch
this, because the defect is invisible until a real client decodes the answer.
So these tests use the evaluator's own client, `psql` over the simple query
protocol (the only PostgreSQL path that uses the cache), and compare the rows
each client actually decodes, in both orders, warm and cold.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.e2e.support import containers
from tests.e2e.support.clients import mcp_call

pytestmark = [pytest.mark.e2e]


def _statement() -> str:
    # A unique literal gives every test its own cache key, so no test depends
    # on the cache being cold or on another test's entries.
    run = uuid.uuid4().hex[:12]
    return f"SELECT id, name, '{run}' AS run FROM customers ORDER BY id LIMIT 3"


def _psql(config: Any, sql: str) -> list[list[str]]:
    """Run `sql` with psql inside the stack, as an agent would, and decode it.

    psql exits non-zero when the proxy sends anything that is not a valid
    wire-protocol response, which is exactly the failure being guarded.
    """
    dsn = (
        f"host=gateway port=5432 user=agent dbname={config.source_id_pg} "
        "sslmode=prefer connect_timeout=10"
    )
    output = containers.exec_in(
        "postgres",
        "env",
        f"PGPASSWORD={config.agent_api_key}",
        "psql",
        dsn,
        "-X",
        "-A",
        "-t",
        "-F",
        "|",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )
    lines = [line for line in output.splitlines() if line.strip()]
    rows = [line.split("|") for line in lines]
    assert rows and all(len(row) == 3 for row in rows), f"psql did not return rows:\n{output}"
    return rows


def _mcp(config: Any, sql: str) -> list[list[str]]:
    response = mcp_call(config, "interlock_query", {"source_id": config.source_id_pg, "sql": sql})
    assert response.status_code == 200, response.text
    payload = response.json()
    text = payload["content"][0]["text"] if "content" in payload else json.dumps(payload["rows"])
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - the failure being guarded
        raise AssertionError(f"MCP returned something that is not JSON: {text[:200]!r}") from exc
    return [[str(row["id"]), str(row["name"]), str(row["run"])] for row in rows]


def test_postgresql_then_mcp_then_postgresql(e2e_config: Any) -> None:
    sql = _statement()
    first = _psql(e2e_config, sql)
    assert _mcp(e2e_config, sql) == first
    # The evaluator's failure: this read found MCP's JSON under the shared key.
    assert _psql(e2e_config, sql) == first


def test_mcp_then_postgresql_then_mcp(e2e_config: Any) -> None:
    sql = _statement()
    first = _mcp(e2e_config, sql)
    assert _psql(e2e_config, sql) == first
    # The reverse: this read found PostgreSQL wire bytes under the shared key.
    assert _mcp(e2e_config, sql) == first


def test_each_protocol_is_served_from_its_own_warm_entry(e2e_config: Any) -> None:
    sql = _statement()
    expected = _psql(e2e_config, sql)
    for _ in range(2):
        assert _psql(e2e_config, sql) == expected
        assert _mcp(e2e_config, sql) == expected
