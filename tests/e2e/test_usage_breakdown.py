"""The Audit page counts requests by outcome, names identities, and prices nothing."""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.support.clients import mcp_call

pytestmark = [pytest.mark.e2e]


@pytest.mark.asyncio
async def test_usage_breakdown_counts_outcomes_by_named_identity(
    e2e_config: Any, admin_session: Any
) -> None:
    mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": "SELECT 1 AS ok"},
    )

    page = admin_session.client.get("/dashboard/audit-costs")
    assert page.status_code == 200
    body = page.text
    assert "Usage breakdown" in body
    assert "InterLock does not price requests" in body
    assert "Total Cost (units)" not in body
    for label in ("Served by the source", "Served from cache", "Denied", "Queued for approval"):
        assert label in body
    assert 'class="usage-seg usage-seg--' in body
    # Identities are named, not bare ids.
    assert "e2e-agent (#" in body

    for key in ("24h", "7d", "30d"):
        part = admin_session.client.get(f"/dashboard/audit-costs/usage?usage_range={key}")
        assert part.status_code == 200
        assert f'value="{key}" selected' in part.text
