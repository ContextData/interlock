"""The source page lists the policies that apply to it, with real match counts."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from tests.e2e.support.clients import mcp_call

pytestmark = [pytest.mark.e2e]


@pytest.mark.asyncio
async def test_policy_panel_counts_the_rule_that_matched_a_request(
    e2e_config: Any, admin_session: Any, control_db: Any
) -> None:
    source_id = e2e_config.source_id_pg
    before = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    response = mcp_call(
        e2e_config, "interlock_query", {"source_id": source_id, "sql": "SELECT 1 AS ok"}
    )
    assert response.status_code == 200, response.text[:300]

    rule_name = None
    for _ in range(40):
        rule_name = await control_db.fetchval(
            """
            SELECT p.name FROM audit_log a
            JOIN policy_rules p ON p.id::text = a.policy_decision->>'rule_id'
            WHERE a.id > $1 AND a.source_id = $2
            ORDER BY a.id DESC LIMIT 1
            """,
            before,
            source_id,
        )
        if rule_name:
            break
        await asyncio.sleep(0.25)
    assert rule_name, "the request's audit row names no policy rule"

    page = admin_session.client.get(f"/dashboard/data-sources/{source_id}").text
    assert "Policies applied to this source" in page
    start = page.index(f">{rule_name}</a>")
    row = page[start : page.index("</tr>", start)]
    counts = re.findall(r'<td class="num">(\d+)</td>', row)
    assert counts and int(counts[-1]) >= 1, row
