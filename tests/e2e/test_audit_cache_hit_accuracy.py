"""The audit says what happened to a cached answer, and can be sliced by protocol.

The rc.13 independent evaluation found three gaps: a redacted answer served
from cache was audited with `pii_detected=False`, the CSV export had neither
the protocol nor the correlation ID, and the audit page had no protocol filter
although the docs promised one. These tests read the rows the gateway wrote,
not its responses.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.e2e.support import containers
from tests.e2e.support.clients import login_admin, mcp_call, wait_for

pytestmark = [pytest.mark.e2e]


def _pii_statement() -> str:
    # The unique literal gives this test its own cache entry.
    return f"SELECT name, email, ssn, '{uuid.uuid4().hex[:12]}' AS run FROM customers LIMIT 2"


async def _latest_hit(control_db: Any, protocol: str, since: datetime) -> Any:
    return await control_db.fetchrow(
        """
        SELECT pii_detected, pii_types, cache_hit, cache_tier
        FROM audit_log
        WHERE protocol = $1 AND cache_hit = TRUE AND created_at >= $2
        ORDER BY created_at DESC LIMIT 1
        """,
        protocol,
        since,
    )


@pytest.mark.asyncio
async def test_a_redacted_mcp_answer_from_cache_is_audited_as_redacted(
    e2e_config: Any, control_db: Any
) -> None:
    since = datetime.now(UTC)
    sql = _pii_statement()
    args = {"source_id": e2e_config.source_id_pg, "sql": sql}
    first = mcp_call(e2e_config, "interlock_query", args)
    second = mcp_call(e2e_config, "interlock_query", args)
    assert first.status_code == second.status_code == 200
    assert "[REDACTED:" in second.text, "the cached answer should still be redacted"

    row = await wait_for(lambda: _latest_hit(control_db, "mcp", since))
    assert row is not None, "no cache-hit audit row for the repeated MCP read"
    assert row["pii_detected"] is True
    assert row["pii_types"], "the cached redaction's PII types were not recorded"


@pytest.mark.asyncio
async def test_a_redacted_postgresql_answer_from_cache_is_audited_as_redacted(
    e2e_config: Any, control_db: Any
) -> None:
    since = datetime.now(UTC)
    sql = _pii_statement()
    dsn = (
        f"host=gateway port=5432 user=agent dbname={e2e_config.source_id_pg} "
        "sslmode=prefer connect_timeout=10"
    )
    for _ in range(2):
        output = containers.exec_in(
            "postgres",
            "env",
            f"PGPASSWORD={e2e_config.agent_api_key}",
            "psql",
            dsn,
            "-X",
            "-A",
            "-t",
            "-c",
            sql,
        )
        assert "[REDACTED:" in output, output

    row = await wait_for(lambda: _latest_hit(control_db, "postgresql", since))
    assert row is not None, "no cache-hit audit row for the repeated psql read"
    assert row["pii_detected"] is True
    assert row["pii_types"]


@pytest.mark.asyncio
async def test_the_export_filters_by_protocol_and_carries_the_correlation_id(
    e2e_config: Any, control_db: Any
) -> None:
    mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": "SELECT 1 AS one"},
    )

    async def has_mcp_row() -> bool:
        count = await control_db.fetchval("SELECT count(*) FROM audit_log WHERE protocol = 'mcp'")
        return bool(count)

    assert await wait_for(has_mcp_row)
    session = login_admin(e2e_config)
    response = session.client.get("/dashboard/audit-costs/export.csv?protocol=mcp&limit=50")
    assert response.status_code == 200, response.text[:300]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows, "the protocol-filtered export is empty"
    assert {row["protocol"] for row in rows} == {"mcp"}
    assert any(row["correlation_id"] for row in rows), "no correlation IDs in the export"
