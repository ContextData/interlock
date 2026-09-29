"""Audit views still name an identity after it has been deleted.

`audit_log` keeps only `identity_id`, and deleting an identity removes its row,
so the console showed a bare `#7` for everything a deleted identity had done.
Deleting through the admin API now leaves a tombstone; the views resolve names
through it.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import pytest

from tests.e2e.support.clients import mcp_call

pytestmark = [pytest.mark.e2e]


async def _audit_row_for(control_db: Any, identity_id: int) -> int | None:
    for _ in range(40):
        audit_id = await control_db.fetchval(
            "SELECT id FROM audit_log WHERE identity_id = $1 ORDER BY id DESC LIMIT 1",
            identity_id,
        )
        if audit_id:
            return int(audit_id)
        await asyncio.sleep(0.25)
    return None


@pytest.mark.asyncio
async def test_a_deleted_identity_is_still_named_in_the_audit_views(
    e2e_config: Any, admin_session: Any, control_db: Any
) -> None:
    name = f"e2e-tombstone-{secrets.token_hex(4)}"
    api_key = f"e2e-tombstone-{secrets.token_urlsafe(32)}"
    headers = {"X-CSRF-Token": admin_session.csrf_token}
    created = admin_session.client.post(
        "/api/identities",
        json={"name": name, "agent_type": "custom", "api_key": api_key},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    identity_id = int(created.json()["id"])

    try:
        # Any authenticated call leaves an audit row with this identity's id;
        # being ungranted, it is refused, which is fine for this purpose.
        mcp_call(
            e2e_config,
            "interlock_query",
            {"source_id": e2e_config.source_id_pg, "sql": "SELECT 1"},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        audit_id = await _audit_row_for(control_db, identity_id)
        assert audit_id, "the request left no audit row for the identity"
    finally:
        deleted = admin_session.client.delete(f"/api/identities/{identity_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    tombstone = await control_db.fetchrow(
        "SELECT name FROM identity_tombstones WHERE identity_id = $1", identity_id
    )
    assert tombstone is not None and tombstone["name"] == name

    expected = f"{name} (#{identity_id}, deleted)"
    event = admin_session.client.get(f"/dashboard/audit-costs/events/{audit_id}")
    assert event.status_code == 200
    assert expected in event.text

    table = admin_session.client.get(f"/dashboard/audit-costs?identity_id={identity_id}")
    assert expected in table.text

    export = admin_session.client.get(
        f"/dashboard/audit-costs/export.csv?identity_id={identity_id}"
    )
    assert export.status_code == 200
    assert any(line.endswith(f",{name}") for line in export.text.splitlines()[1:])
