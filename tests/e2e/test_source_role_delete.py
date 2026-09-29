"""A source role can be deleted, but never out from under an identity holding it.

Deleting a role cascades through `identity_source_role_grants`, so a delete
that ignored active grants would silently revoke them. The console therefore
refuses while any grant is active; once they are revoked, the role goes and
the admin audit log keeps the role, its statements and its grant history.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

pytestmark = [pytest.mark.e2e]


def _headers(admin_session: Any, *, htmx: bool = False) -> dict[str, str]:
    headers = {"X-CSRF-Token": admin_session.csrf_token}
    if htmx:
        headers["HX-Request"] = "true"
    return headers


@pytest.fixture
async def scratch_role(e2e_config: Any, admin_session: Any, control_db: Any) -> Any:
    source_id = e2e_config.source_id_pg
    role_key = f"e2e_delete_{secrets.token_hex(3)}"
    created = admin_session.client.post(
        f"/dashboard/data-sources/{source_id}/roles",
        data={
            "role_key": role_key,
            "name": role_key,
            "enabled": "on",
            "permission_effect": "allow",
            "permission_action": "db.table.select",
            "permission_resource_type": "db.table",
            "permission_resource_pattern": "public.customers.*",
            "permission_constraints": "{}",
        },
        headers=_headers(admin_session),
    )
    assert created.status_code in (200, 303), created.text[:300]
    role_id = await control_db.fetchval(
        "SELECT id FROM source_roles WHERE source_id = $1 AND role_key = $2",
        source_id,
        role_key,
    )
    assert role_id, "the scratch role was not created"
    yield source_id, int(role_id), role_key
    await control_db.execute("DELETE FROM source_roles WHERE id = $1", role_id)


@pytest.fixture
async def scratch_identity(admin_session: Any, control_db: Any) -> Any:
    created = admin_session.client.post(
        "/api/identities",
        json={
            "name": f"e2e-role-delete-{secrets.token_hex(4)}",
            "agent_type": "custom",
            "api_key": f"e2e-role-delete-{secrets.token_urlsafe(32)}",
        },
        headers=_headers(admin_session),
    )
    assert created.status_code == 201, created.text
    identity_id = int(created.json()["id"])
    yield identity_id
    await control_db.execute("DELETE FROM identities WHERE id = $1", identity_id)


@pytest.fixture
async def naming_policy(admin_session: Any, control_db: Any, scratch_role: Any) -> Any:
    _source_id, _role_id, role_key = scratch_role
    name = f"e2e-names-{role_key}"
    created = admin_session.client.post(
        "/api/policies",
        json={
            "name": name,
            "priority": 1,
            "enabled": False,
            "conditions": {"roles": [role_key]},
            "actions": {"effect": "deny"},
        },
        headers=_headers(admin_session),
    )
    assert created.status_code in (200, 201), created.text
    yield name
    await control_db.execute("DELETE FROM policy_rules WHERE name = $1", name)


@pytest.mark.asyncio
async def test_a_granted_role_cannot_be_deleted_until_its_grants_are_revoked(
    admin_session: Any,
    control_db: Any,
    scratch_role: Any,
    scratch_identity: int,
    naming_policy: str,
) -> None:
    source_id, role_id, role_key = scratch_role
    granted = admin_session.client.post(
        f"/api/identities/{scratch_identity}/source-role-grants",
        json={"source_id": source_id, "role_id": role_id},
        headers=_headers(admin_session),
    )
    assert granted.status_code == 201, granted.text
    grant_id = int(granted.json()["id"])

    detail = admin_session.client.get(f"/dashboard/data-sources/{source_id}")
    assert "Revoke the grants before deleting." in detail.text

    refused = admin_session.client.post(
        f"/dashboard/data-sources/{source_id}/roles/{role_id}/delete",
        headers=_headers(admin_session, htmx=True),
    )
    assert refused.status_code == 200
    assert f"Role {role_key} is still granted to" in refused.text
    assert await control_db.fetchval("SELECT 1 FROM source_roles WHERE id = $1", role_id)
    assert await control_db.fetchval(
        "SELECT enabled FROM identity_source_role_grants WHERE id = $1", grant_id
    )

    revoked = admin_session.client.delete(
        f"/api/identities/{scratch_identity}/source-role-grants/{grant_id}",
        headers=_headers(admin_session),
    )
    assert revoked.status_code == 200, revoked.text

    deleted = admin_session.client.post(
        f"/dashboard/data-sources/{source_id}/roles/{role_id}/delete",
        headers=_headers(admin_session, htmx=True),
    )
    assert deleted.status_code == 200
    assert deleted.headers.get("HX-Redirect") == f"/dashboard/data-sources/{source_id}"
    assert await control_db.fetchval("SELECT 1 FROM source_roles WHERE id = $1", role_id) is None
    assert (
        await control_db.fetchval(
            "SELECT 1 FROM source_role_permissions WHERE role_id = $1", role_id
        )
        is None
    )

    audit = await control_db.fetchrow(
        """
        SELECT detail FROM admin_audit_log
        WHERE action = 'source_role.delete' AND resource_id = $1 AND success = TRUE
        ORDER BY id DESC LIMIT 1
        """,
        str(role_id),
    )
    assert audit is not None, "the delete left no admin audit entry"
    detail_json = audit["detail"]
    if isinstance(detail_json, str):
        detail_json = json.loads(detail_json)
    before = detail_json["before"]
    assert before["role_key"] == role_key
    assert before["permissions"][0]["resource_pattern"] == "public.customers.*"
    assert [g["id"] for g in before["grants"]] == [grant_id]
    assert before["grants"][0]["enabled"] is False
    assert detail_json["referencing_policies"] == [naming_policy]
