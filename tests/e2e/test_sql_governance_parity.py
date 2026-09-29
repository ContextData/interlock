"""The console's dry-run explains the decision the gateway actually makes.

Each statement is sent through the gateway over MCP, and the permission request
governance evaluated is read back from the audit row. The same statement is
then run through the source-role dry-run for the same identity. Action and
resources must match exactly: a dry-run that disagrees tells an operator their
roles are broken when they are not, which is what the console dry-run once did for
every SQL statement.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from typing import Any

import pytest

from tests.e2e.support.clients import mcp_call

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("catalogued_sql_sources")]

_READS = (
    "SELECT name FROM customers WHERE id = 1",
    "SELECT c.name, o.id FROM customers c JOIN orders o ON o.customer_id = c.id",
    "WITH recent AS (SELECT id FROM orders) SELECT count(*) FROM recent",
    "SELECT * FROM customers WHERE id IN (SELECT customer_id FROM orders)",
    "SELECT count(*) FROM orders",
)


def _dry_run(admin: Any, source_id: str, identity_id: int, sql: str) -> tuple[str, list[str]]:
    response = admin.client.post(
        f"/dashboard/data-sources/{source_id}/roles/dry-run",
        data={"identity_id": str(identity_id), "sql": sql},
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    assert response.status_code == 200, response.text[:300]
    body = response.text
    action = re.search(r"<dt>Action</dt><dd><code>([^<]*)</code>", body)
    resources = re.search(r"<dt>Resources</dt>\s*<dd>(.*?)</dd>", body, re.S)
    assert action and resources, body[:600]
    return (
        html.unescape(action.group(1)),
        [html.unescape(r) for r in re.findall(r"<code>([^<]*)</code>", resources.group(1))],
    )


async def _audited(
    control_db: Any, source_id: str, since: Any, protocol: str = "mcp"
) -> dict[str, Any]:
    # The gateway's audit buffer flushes in the background.
    row = None
    for _ in range(40):
        row = await control_db.fetchrow(
            """
            SELECT identity_id, request_metadata FROM audit_log
            WHERE source_id = $1 AND protocol = $3 AND created_at >= $2
            ORDER BY created_at DESC LIMIT 1
            """,
            source_id,
            since,
            protocol,
        )
        if row is not None:
            break
        await asyncio.sleep(0.25)
    assert row is not None, "the gateway never audited the request"
    metadata = row["request_metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    request = metadata.get("permission_request")
    assert request, metadata
    return {"identity_id": row["identity_id"], **request}


@pytest.mark.parametrize("source", ["pg", "mysql"])
@pytest.mark.parametrize("sql", _READS)
async def test_the_dry_run_derives_what_the_gateway_evaluates(
    source: str, sql: str, admin_session: Any, control_db: Any, e2e_config: Any
) -> None:
    source_id = e2e_config.source_id_pg if source == "pg" else e2e_config.source_id_mysql
    since = await control_db.fetchval("SELECT NOW()")
    response = mcp_call(e2e_config, "interlock_query", {"source_id": source_id, "sql": sql})
    assert response.status_code in (200, 403), response.text
    gateway = await _audited(control_db, source_id, since)

    action, resources = _dry_run(admin_session, source_id, gateway["identity_id"], sql)
    assert (action, sorted(resources)) == (gateway["action"], sorted(gateway["resources"]))


async def test_the_naming_report_finds_legacy_public_roles(
    admin_session: Any, e2e_config: Any
) -> None:
    response = admin_session.client.get("/api/catalog/naming-report")
    assert response.status_code == 200, response.text[:300]
    findings = response.json()["findings"]
    legacy = [
        f
        for f in findings
        if f["source_id"] == e2e_config.source_id_mysql and f["kind"] == "legacy_alias"
    ]
    assert legacy, findings
    assert {f["suggestion"] for f in legacy} >= {"source_db.customers.*"}


@pytest.mark.parametrize("sql", _READS)
async def test_the_pg_wire_derives_what_the_dry_run_explains(
    sql: str, admin_session: Any, control_db: Any, e2e_config: Any
) -> None:
    import asyncpg

    since = await control_db.fetchval("SELECT NOW()")
    conn = await asyncpg.connect(**e2e_config.gateway_pg_params_api_key)
    try:
        await conn.fetch(sql)
    finally:
        await conn.close()
    gateway = await _audited(control_db, e2e_config.source_id_pg, since, protocol="postgresql")
    # The PG wire resolves names from the catalog, as MCP does.
    assert gateway.get("naming_resolved") is True, gateway

    action, resources = _dry_run(
        admin_session, e2e_config.source_id_pg, gateway["identity_id"], sql
    )
    assert (action, sorted(resources)) == (gateway["action"], sorted(gateway["resources"]))
