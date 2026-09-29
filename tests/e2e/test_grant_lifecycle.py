"""An agent can gain and lose a source without ever being re-keyed.

Grants were writable in exactly one
place - the identity *create* handler - so the only way to give an existing
agent access to a newly registered source was to delete the identity and
recreate it. Creation mints a fresh API key, shown once, so every deployed
client holding the old key stopped working. "You cannot grant an existing
agent access to a new source" is a gap in a product whose premise is governed
access to data sources.

The proof has to be behavioural rather than a status column: the same key,
unchanged, denied then permitted then denied again, with each answer read from
what the gateway actually does with a request.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from tests.e2e.support.clients import mcp_call

pytestmark = [pytest.mark.e2e]

_QUERY = "SELECT 1"


# The role keys the stack's allow policy recognises (tests/e2e/support/seed.py).
_POLICY_ALLOWED_ROLE_KEYS = [
    "analyst",
    "writer",
    "owner",
    "reader",
    "query_reader",
    "collection_reader",
    "object_reader",
    "workspace_reader",
]


async def _grantable_role_id(control_db: Any, source_id: str) -> int:
    role_id = await control_db.fetchval(
        """
        SELECT id FROM source_roles
        WHERE source_id = $1 AND enabled = TRUE AND role_key = ANY($2::text[])
        ORDER BY id LIMIT 1
        """,
        source_id,
        _POLICY_ALLOWED_ROLE_KEYS,
    )
    assert role_id, f"no policy-recognised source role exists on {source_id} to grant"
    return int(role_id)


def _call_as(e2e_config: Any, api_key: str, source_id: str) -> Any:
    return mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": source_id, "sql": _QUERY},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )


def _denied(response: Any) -> bool:
    """A refusal, however the protocol spells it."""
    if response.status_code in (401, 403):
        return True
    body = response.text.lower()
    return "denied" in body or "no source role" in body


@pytest.fixture
async def throwaway_identity(admin_session: Any, control_db: Any) -> Any:
    """An identity with a known key and deliberately no grants."""
    api_key = f"e2e-grant-{secrets.token_urlsafe(32)}"
    created = admin_session.client.post(
        "/api/identities",
        json={
            "name": f"e2e-grant-lifecycle-{secrets.token_hex(4)}",
            "agent_type": "custom",
            "api_key": api_key,
        },
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert created.status_code == 201, created.text
    identity_id = int(created.json()["id"])
    yield identity_id, api_key
    await control_db.execute("DELETE FROM identities WHERE id = $1", identity_id)


@pytest.mark.asyncio
async def test_a_grant_and_a_revoke_never_change_the_agents_key(
    e2e_config: Any, admin_session: Any, control_db: Any, throwaway_identity: Any
) -> None:
    identity_id, api_key = throwaway_identity
    source_id = e2e_config.source_id_pg

    # The stack's allow policy matches on SOURCE-scoped role keys (policy.py:194
    # reads the identity's grants for this source, not its legacy labels), so
    # the granted role has to be one the policy recognises. Otherwise the
    # request is refused by the policy layer and the grant's effect is masked.
    role_id = await _grantable_role_id(control_db, source_id)

    key_before = await control_db.fetchval(
        "SELECT api_key_hash FROM identities WHERE id = $1", identity_id
    )

    # 1. Ungranted: the key authenticates but reaches nothing.
    assert _denied(_call_as(e2e_config, api_key, source_id)), "an ungranted identity was served"

    # 2. Grant, without touching the credential.
    granted = admin_session.client.post(
        f"/api/identities/{identity_id}/source-role-grants",
        json={"source_id": source_id, "role_id": int(role_id)},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert granted.status_code == 201, granted.text
    grant_id = int(granted.json()["id"])

    # 3. The same key - never rotated - now reaches the source.
    allowed = _call_as(e2e_config, api_key, source_id)
    assert not _denied(allowed), f"the granted identity was still refused: {allowed.text[:300]}"

    # 4. Revoke, and the very next request is refused again.
    revoked = admin_session.client.delete(
        f"/api/identities/{identity_id}/source-role-grants/{grant_id}",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["enabled"] is False

    assert _denied(
        _call_as(e2e_config, api_key, source_id)
    ), "a revoked grant still served the agent"

    # The credential is the thing that must not have moved.
    key_after = await control_db.fetchval(
        "SELECT api_key_hash FROM identities WHERE id = $1", identity_id
    )
    assert key_after == key_before, "the agent was re-keyed by a grant change"


@pytest.mark.asyncio
async def test_a_revoked_grant_is_kept_as_history_not_deleted(
    e2e_config: Any, admin_session: Any, control_db: Any, throwaway_identity: Any
) -> None:
    """An incident review asks whether an agent ever held access, and when it
    was withdrawn. A deleted row cannot answer that."""
    identity_id, _api_key = throwaway_identity
    source_id = e2e_config.source_id_pg
    role_id = await _grantable_role_id(control_db, source_id)

    granted = admin_session.client.post(
        f"/api/identities/{identity_id}/source-role-grants",
        json={"source_id": source_id, "role_id": int(role_id)},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    grant_id = int(granted.json()["id"])
    admin_session.client.delete(
        f"/api/identities/{identity_id}/source-role-grants/{grant_id}",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )

    row = await control_db.fetchrow(
        "SELECT enabled, granted_by FROM identity_source_role_grants WHERE id = $1", grant_id
    )
    assert row is not None, "the revoked grant row was deleted"
    assert row["enabled"] is False
    # granted_by had never been written by any code path before this change.
    assert row["granted_by"] is not None, "the acting administrator was not recorded"

    listed = admin_session.client.get(f"/api/identities/{identity_id}/source-role-grants").json()
    assert listed == [], "a revoked grant is history, not access"

    with_history = admin_session.client.get(
        f"/api/identities/{identity_id}/source-role-grants?include_revoked=true"
    ).json()
    assert [g["id"] for g in with_history] == [grant_id]
