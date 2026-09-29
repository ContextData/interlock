"""Authorization guarantees, proven rather than assumed.

Phase 1 of the governance audit. The mutation runner
(``tools/audit/mutate.py``) showed that three documented authorization
guarantees could be removed from the source without any test objecting:

- an explicit deny beats an overlapping allow
- an expired grant confers nothing
- an identity with no grant on a source is denied

Each was investigated before being written up, and in every case the control
itself was working - what was missing was proof. Two of the three survived
their mutation only because a second check downstream preserved the *outcome*
while changing the reason, which is defence in depth working as intended but
also means no test could tell the difference. The expiry case was verified by
hand against the running stack: expiring a grant produced
``No source role grants matched this identity and source`` and restoring it
returned access.

The gap those mutations exposed is that no test configuration anywhere grants
an identity an allow and a deny over the same resource, which is the only
arrangement where "deny wins" means anything.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import pytest

from interlock.core.auth import AuthManager
from tests.e2e.support import effects
from tests.e2e.support.clients import http_proxy_request

pytestmark = [pytest.mark.e2e]

_SEED_TAG = "authz_audit"
_POLICY_LABEL = "authz_audit_agent"


async def _role_with(
    conn: Any, *, source_id: str, role_key: str, permissions: list[dict[str, Any]]
) -> int:
    role_id = await conn.fetchval(
        """
        INSERT INTO source_roles (source_id, role_key, name, description, metadata)
        VALUES ($1, $2, initcap(replace($2, '_', ' ')), 'authz audit role', $3::jsonb)
        ON CONFLICT (source_id, role_key) DO UPDATE
        SET enabled = TRUE, updated_at = NOW()
        RETURNING id
        """,
        source_id,
        role_key,
        json.dumps({"seed": _SEED_TAG}),
    )
    await conn.execute("DELETE FROM source_role_permissions WHERE role_id = $1", role_id)
    for permission in permissions:
        await conn.execute(
            """
            INSERT INTO source_role_permissions
                (role_id, effect, action, resource_type, resource_pattern, constraints)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            role_id,
            permission["effect"],
            permission["action"],
            permission["resource_type"],
            permission["resource_pattern"],
            json.dumps(permission.get("constraints") or {}),
        )
    return int(role_id)


async def _identity_with_grants(
    conn: Any,
    *,
    name: str,
    api_key: str,
    source_id: str,
    role_ids: list[int],
    expires_at: Any = None,
    policy_label: str = _POLICY_LABEL,
) -> int:
    """Create an identity whose access comes from its source-role grants.

    `policy_label` populates the legacy ``identities.roles`` column. That column
    is not authority for access - source roles are - but organization policy
    rules match on it, and the seeded allow-rule matches these labels. Without
    one, every request here is refused by policy default-deny before the
    source-role decision can be observed, which is the layering these tests
    need to see past rather than a control they are testing.
    """
    identity_id = await conn.fetchval(
        """
        INSERT INTO identities
            (name, api_key_hash, agent_type, team, roles, enabled, metadata)
        VALUES ($1, $2, 'codex', 'e2e', $3::text[], TRUE, $4::jsonb)
        ON CONFLICT (api_key_hash) DO UPDATE
        SET name = EXCLUDED.name, enabled = TRUE, updated_at = NOW()
        RETURNING id
        """,
        name,
        AuthManager.hash_api_key(api_key),
        [policy_label],
        json.dumps({"seed": _SEED_TAG}),
    )
    for role_id in role_ids:
        await conn.execute(
            """
            INSERT INTO identity_source_role_grants
                (identity_id, source_id, role_id, expires_at, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (identity_id, source_id, role_id) DO UPDATE
            SET enabled = TRUE, expires_at = EXCLUDED.expires_at, updated_at = NOW()
            """,
            identity_id,
            source_id,
            role_id,
            expires_at,
            json.dumps({"seed": _SEED_TAG}),
        )
    return int(identity_id)


async def _allow_policy(conn: Any, source_id: str) -> None:
    """Give the audit identities their own organization policy rule.

    Source-role allow is necessary but not sufficient: policy runs afterwards
    and default-denies anything no rule matches. Rather than depend on the
    seeded rule's condition set, these tests carry their own, so a change to
    the seed cannot silently turn an authorization test into a policy test.
    """
    await conn.execute("DELETE FROM policy_rules WHERE name = $1", _SEED_TAG)
    await conn.execute(
        """
        INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
        VALUES ($1, 5, $2::jsonb, $3::jsonb, TRUE)
        """,
        _SEED_TAG,
        json.dumps({"source_ids": [source_id], "roles": [_POLICY_LABEL]}),
        json.dumps({"effect": "allow"}),
    )
    # The gateway caches policy and reloads on NOTIFY; without this the rule
    # exists in the database but the running process never sees it.
    await conn.execute("SELECT pg_notify('onyx_policy_changed', $1)", _SEED_TAG)
    await asyncio.sleep(0.5)


async def _cleanup(conn: Any) -> None:
    await conn.execute("DELETE FROM policy_rules WHERE name = $1", _SEED_TAG)
    await conn.execute(
        "DELETE FROM identity_source_role_grants WHERE metadata->>'seed' = $1", _SEED_TAG
    )
    await conn.execute(
        "DELETE FROM source_role_permissions WHERE role_id IN"
        " (SELECT id FROM source_roles WHERE metadata->>'seed' = $1)",
        _SEED_TAG,
    )
    await conn.execute("DELETE FROM source_roles WHERE metadata->>'seed' = $1", _SEED_TAG)
    await conn.execute("DELETE FROM identities WHERE metadata->>'seed' = $1", _SEED_TAG)


@pytest.fixture
async def authz_fixtures(control_db: Any, e2e_config: Any) -> Any:
    await _cleanup(control_db)
    await _allow_policy(control_db, e2e_config.source_id_http)
    try:
        yield control_db
    finally:
        await _cleanup(control_db)


# The resource a source-role statement sees is the full proxy path, e.g.
# "/proxy/e2e_http/json/customer" - not the upstream path. These use the same
# wildcard shape as the seeded `owner` role rather than encoding glob
# semantics the audit is not trying to pin down here.
_READ_HTTP = {
    "effect": "allow",
    "action": "*",
    "resource_type": "*",
    "resource_pattern": "*",
}
_DENY_HTTP = {
    "effect": "deny",
    "action": "*",
    "resource_type": "*",
    "resource_pattern": "*",
}


@pytest.mark.asyncio
async def test_explicit_deny_beats_an_overlapping_allow(
    e2e_config: Any, authz_fixtures: Any
) -> None:
    """The guarantee the docs make, in the only arrangement where it means anything.

    No other test grants an identity both an allow and a deny over the same
    resource, so removing the deny branch entirely left every test passing:
    a blocked identity has no allow to fall back on, so it stayed denied for a
    different reason. Here the allow is real, and only the deny can stop it.
    """
    conn = authz_fixtures
    api_key = f"authz-deny-{secrets.token_urlsafe(16)}"

    allow_role = await _role_with(
        conn, source_id=e2e_config.source_id_http, role_key="authz_allow", permissions=[_READ_HTTP]
    )
    deny_role = await _role_with(
        conn, source_id=e2e_config.source_id_http, role_key="authz_deny", permissions=[_DENY_HTTP]
    )
    await _identity_with_grants(
        conn,
        name="authz-overlap-agent",
        api_key=api_key,
        source_id=e2e_config.source_id_http,
        role_ids=[allow_role, deny_role],
    )

    response = http_proxy_request(
        e2e_config,
        "GET",
        "json/customer",
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert response.status_code == 403, (
        "an overlapping deny must beat the allow; got "
        f"{response.status_code}: {response.text[:200]}"
    )
    assert "Explicit deny" in response.text, (
        "denied for the wrong reason - the deny branch is not what stopped this: "
        f"{response.text[:200]}"
    )


@pytest.mark.asyncio
async def test_an_expired_grant_confers_nothing(e2e_config: Any, control_db: Any) -> None:
    """An expired grant must deny, and the same grant unexpired must allow.

    Uses the seeded agent rather than a synthetic identity on purpose. Access
    here depends on source roles *and* organization policy, and a purpose-built
    identity has to satisfy both before the grant lifecycle can be observed at
    all - which turns an authorization test into a fixture test. The seeded
    agent is already known to be allowed, so expiring its grant isolates the
    one variable under test. Restored in `finally` regardless of outcome.
    """
    path = "json/customer"
    headers = {"Authorization": f"Bearer {e2e_config.agent_api_key}"}

    assert (
        http_proxy_request(e2e_config, "GET", path, headers=headers).status_code == 200
    ), "precondition failed: the seeded agent should already have access"
    try:
        await control_db.execute(
            "UPDATE identity_source_role_grants SET expires_at = NOW() - INTERVAL '1 hour'"
            " WHERE source_id = $1 AND identity_id ="
            " (SELECT id FROM identities WHERE name = 'e2e-agent')",
            e2e_config.source_id_http,
        )
        expired = http_proxy_request(e2e_config, "GET", path, headers=headers)
        assert (
            expired.status_code == 403
        ), f"an expired grant still allowed access: {expired.status_code} {expired.text[:200]}"
        assert (
            "No source role grants matched" in expired.text
        ), f"denied, but not because the grant expired: {expired.text[:200]}"
    finally:
        await control_db.execute(
            "UPDATE identity_source_role_grants SET expires_at = NULL"
            " WHERE source_id = $1 AND identity_id ="
            " (SELECT id FROM identities WHERE name = 'e2e-agent')",
            e2e_config.source_id_http,
        )

    assert http_proxy_request(e2e_config, "GET", path, headers=headers).status_code == 200, (
        "access was not restored when the grant was un-expired, so the denial above"
        " does not isolate expiry"
    )


@pytest.mark.asyncio
async def test_a_disabled_grant_confers_nothing(e2e_config: Any, control_db: Any) -> None:
    """Disabling a grant must take effect on the very next request."""
    path = "json/customer"
    headers = {"Authorization": f"Bearer {e2e_config.agent_api_key}"}

    assert http_proxy_request(e2e_config, "GET", path, headers=headers).status_code == 200
    try:
        await control_db.execute(
            "UPDATE identity_source_role_grants SET enabled = FALSE"
            " WHERE source_id = $1 AND identity_id ="
            " (SELECT id FROM identities WHERE name = 'e2e-agent')",
            e2e_config.source_id_http,
        )
        revoked = http_proxy_request(e2e_config, "GET", path, headers=headers)
        assert (
            revoked.status_code == 403
        ), f"a disabled grant still allowed access: {revoked.text[:200]}"
    finally:
        await control_db.execute(
            "UPDATE identity_source_role_grants SET enabled = TRUE"
            " WHERE source_id = $1 AND identity_id ="
            " (SELECT id FROM identities WHERE name = 'e2e-agent')",
            e2e_config.source_id_http,
        )

    assert (
        http_proxy_request(e2e_config, "GET", path, headers=headers).status_code == 200
    ), "access was not restored after re-enabling the grant"


@pytest.mark.asyncio
async def test_a_denied_write_leaves_the_upstream_untouched(
    e2e_config: Any, authz_fixtures: Any
) -> None:
    """Authorization is judged by effect, not by the refusal message.

    A 403 proves the gateway said no. It does not prove nothing reached the
    origin. This checks the origin's own call log, which is the same class of
    evidence whose absence let the Write Safety defect through.
    """
    conn = authz_fixtures
    api_key = f"authz-write-{secrets.token_urlsafe(16)}"
    read_only = await _role_with(
        conn,
        source_id=e2e_config.source_id_http,
        role_key="authz_read_only",
        permissions=[_READ_HTTP],
    )
    await _identity_with_grants(
        conn,
        name="authz-readonly-agent",
        api_key=api_key,
        source_id=e2e_config.source_id_http,
        role_ids=[read_only],
    )

    probe_path = "mutation/authz-audit-probe"
    before = len(effects.http_upstream_calls(e2e_config, method="DELETE", path=probe_path))

    response = http_proxy_request(
        e2e_config,
        "DELETE",
        probe_path,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code in (
        403,
        202,
    ), f"a read-only grant must not permit a DELETE outright: {response.status_code}"

    after = len(effects.http_upstream_calls(e2e_config, method="DELETE", path=probe_path))
    assert after == before, (
        "the gateway refused the write but the request still reached the origin: "
        f"{before} -> {after} DELETE calls"
    )


def test_describe_access_tells_an_agent_what_the_evaluator_will_enforce(
    e2e_config,
) -> None:
    """The capability surface has to agree with the gate, or it is worse than nothing.

    An agent that plans from `interlock_describe_access` and is then refused
    would be misled into treating governance as unreliable. So this asserts the
    pairing directly: an action the tool advertises succeeds, and the source it
    omits for a blocked identity is one that identity genuinely cannot read.
    """
    from tests.e2e.support.clients import denied_headers, mcp_call

    described = mcp_call(e2e_config, "interlock_describe_access", {})
    assert described.status_code == 200
    access = json.loads(described.json()["content"][0]["text"])

    pg = access[e2e_config.source_id_pg]
    advertised = {entry["action"] for entry in pg["allow"]}
    assert "db.table.select" in advertised, pg

    # Advertised means attemptable: the same identity's select actually works.
    queried = mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": "SELECT 1 AS ok"},
    )
    assert queried.status_code == 200, queried.text

    # A narrowed request answers about that source only.
    narrowed = mcp_call(
        e2e_config,
        "interlock_describe_access",
        {"source_id": e2e_config.source_id_pg},
    )
    assert set(json.loads(narrowed.json()["content"][0]["text"])) == {e2e_config.source_id_pg}

    # The blocked identity is told nothing about the source, and equally cannot
    # read it - the omission is honest rather than a hidden capability.
    blocked = mcp_call(
        e2e_config,
        "interlock_describe_access",
        {},
        headers=denied_headers(e2e_config),
    )
    assert blocked.status_code == 200
    blocked_access = json.loads(blocked.json()["content"][0]["text"])
    assert e2e_config.source_id_pg not in blocked_access

    refused = mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": "SELECT 1 AS ok"},
        headers=denied_headers(e2e_config),
    )
    assert refused.status_code in (401, 403), refused.text


def test_describe_access_requires_a_caller(e2e_config) -> None:
    """Anonymous callers learn nothing about anyone's authority."""
    from tests.e2e.support.clients import mcp_call

    resp = mcp_call(
        e2e_config,
        "interlock_describe_access",
        {},
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 401
