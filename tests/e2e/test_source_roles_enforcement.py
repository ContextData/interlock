"""Source-scoped IAM role enforcement E2E tests."""

from __future__ import annotations

import json

import pytest

from interlock.admin.auth import hash_password
from interlock.core.auth import AuthManager
from tests.e2e.support.clients import http_proxy_request, wait_for


async def _seed_role(
    conn,
    *,
    source_id: str,
    role_key: str,
    permission: dict,
) -> int:
    role_id = await conn.fetchval(
        """
        INSERT INTO source_roles
            (source_id, role_key, name, description, metadata)
        VALUES ($1, $2, initcap(replace($2, '_', ' ')),
                'E2E limited source role', $3::jsonb)
        ON CONFLICT (source_id, role_key) DO UPDATE
        SET name = EXCLUDED.name,
            description = EXCLUDED.description,
            enabled = TRUE,
            updated_at = NOW()
        RETURNING id
        """,
        source_id,
        role_key,
        json.dumps({"seed": "e2e", "limited": True}),
    )
    await conn.execute("DELETE FROM source_role_permissions WHERE role_id = $1", role_id)
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
        json.dumps(permission.get("constraints", {})),
    )
    policy_name = f"e2e-allow-{role_key}"
    await conn.execute("DELETE FROM policy_rules WHERE name = $1", policy_name)
    await conn.execute(
        """
        INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
        VALUES ($1, 50, $2::jsonb, $3::jsonb, TRUE)
        """,
        policy_name,
        json.dumps(
            {
                "source_ids": [source_id],
                "roles": [role_key],
                "operation_types": ["read", "write", "discovery"],
                "role_semantics": "source_role_keys",
            }
        ),
        json.dumps({"effect": "allow", "redact_columns": ["ssn", "email"]}),
    )
    await conn.execute("SELECT pg_notify('onyx_policy_changed', $1)", "{}")
    return int(role_id)


async def _seed_identity_with_grant(
    conn,
    *,
    name: str,
    api_key: str,
    roles: list[str],
    source_id: str,
    role_id: int,
    pg_username: str | None = None,
    pg_password: str | None = None,
) -> int:
    identity_id = await conn.fetchval(
        """
        INSERT INTO identities
            (name, api_key_hash, agent_type, team, roles, enabled, metadata,
             pg_username, pg_password_hash, auth_metadata)
        VALUES ($1, $2, 'codex', 'e2e', $3::text[], TRUE, $4::jsonb,
                $5, $6, $7::jsonb)
        ON CONFLICT (api_key_hash) DO UPDATE
        SET name = EXCLUDED.name,
            roles = EXCLUDED.roles,
            enabled = TRUE,
            metadata = EXCLUDED.metadata,
            pg_username = EXCLUDED.pg_username,
            pg_password_hash = EXCLUDED.pg_password_hash,
            updated_at = NOW()
        RETURNING id
        """,
        name,
        AuthManager.hash_api_key(api_key),
        roles,
        json.dumps({"seed": "e2e", "source_role_e2e": True}),
        pg_username,
        hash_password(pg_password) if pg_password else None,
        json.dumps({"seed": "e2e"}),
    )
    await conn.execute(
        """
        INSERT INTO identity_source_role_grants
            (identity_id, source_id, role_id, metadata)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (identity_id, source_id, role_id) DO UPDATE
        SET enabled = TRUE,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        identity_id,
        source_id,
        role_id,
        json.dumps({"seed": "e2e", "source_role_e2e": True}),
    )
    return int(identity_id)


def _metadata(row) -> dict:
    value = row["request_metadata"]
    for _ in range(3):
        if isinstance(value, str):
            value = json.loads(value)
            continue
        break
    metadata = dict(value or {})
    for key in ("source_role_decision", "permission_request"):
        if isinstance(metadata.get(key), str):
            metadata[key] = json.loads(metadata[key])
    return metadata


async def _latest_denied_for_identity(conn, identity_id: int, *, after_id: int = 0):
    return await conn.fetchrow(
        """
        SELECT id, request_metadata
        FROM audit_log
        WHERE identity_id = $1
          AND status = 'denied'
          AND id > $2
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        identity_id,
        after_id,
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pg_source_role_limited_to_one_table(e2e_config, control_db) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    pg_password = "limited-source-role-password"
    role_id = await _seed_role(
        control_db,
        source_id=e2e_config.source_id_pg,
        role_key="customers_only_e2e",
        permission={
            "effect": "allow",
            "action": "db.table.select",
            "resource_type": "db.table",
            "resource_pattern": "public.customers.*",
        },
    )
    identity_id = await _seed_identity_with_grant(
        control_db,
        name="e2e-source-role-pg",
        api_key="ag-e2e-source-role-pg",
        roles=["analyst"],
        source_id=e2e_config.source_id_pg,
        role_id=role_id,
        pg_username="e2e_limited_pg_user",
        pg_password=pg_password,
    )

    before_denied_id = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    conn = await asyncpg.connect(
        host=e2e_config.gateway_pg_params_password["host"],
        port=e2e_config.gateway_pg_params_password["port"],
        user="e2e_limited_pg_user",
        password=pg_password,
        database=e2e_config.source_id_pg,
        timeout=10,
    )
    try:
        assert await conn.fetchval("SELECT name FROM customers WHERE id = 1") == "Ada Lovelace"
        with pytest.raises(asyncpg.PostgresError):
            await conn.fetchval("SELECT status FROM orders WHERE id = 100")
    finally:
        await conn.close()

    row = await wait_for(
        lambda: _latest_denied_for_identity(control_db, identity_id, after_id=before_denied_id)
    )
    metadata = _metadata(row)
    assert metadata["source_role_decision"]["allowed"] is False
    assert "db.table.select" in json.dumps(metadata["permission_request"])


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_source_role_limited_to_get_customer_path(
    e2e_config,
    control_db,
    http_upstream,
) -> None:
    api_key = "ag-e2e-source-role-http"
    role_id = await _seed_role(
        control_db,
        source_id=e2e_config.source_id_http,
        role_key="customer_get_e2e",
        permission={
            "effect": "allow",
            "action": "http.get",
            "resource_type": "http.path",
            "resource_pattern": "/json/customer",
        },
    )
    identity_id = await _seed_identity_with_grant(
        control_db,
        name="e2e-source-role-http",
        api_key=api_key,
        roles=["writer"],
        source_id=e2e_config.source_id_http,
        role_id=role_id,
    )

    before_denied_id = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    allowed = http_proxy_request(
        e2e_config,
        "GET",
        "/json/customer",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    denied = http_proxy_request(
        e2e_config,
        "GET",
        "/text/customer",
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert allowed.status_code == 200
    assert denied.status_code == 403

    row = await wait_for(
        lambda: _latest_denied_for_identity(control_db, identity_id, after_id=before_denied_id)
    )
    metadata = _metadata(row)
    assert metadata["source_role_decision"]["allowed"] is False
    assert metadata["permission_request"]["path"] == "/text/customer"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_legacy_metadata_role_cannot_grant_source_access(
    e2e_config,
    control_db,
    http_upstream,
) -> None:
    api_key = "ag-e2e-legacy-metadata-only"
    role_id = await _seed_role(
        control_db,
        source_id=e2e_config.source_id_http,
        role_key="metadata_only_e2e",
        permission={
            "effect": "allow",
            "action": "http.get",
            "resource_type": "http.path",
            "resource_pattern": "/json/customer",
        },
    )
    identity_id = await control_db.fetchval(
        """
        INSERT INTO identities
            (name, api_key_hash, agent_type, team, roles, enabled, metadata,
             auth_metadata)
        VALUES ($1, $2, 'codex', 'e2e', $3::text[], TRUE, $4::jsonb,
                $5::jsonb)
        ON CONFLICT (api_key_hash) DO UPDATE
        SET name = EXCLUDED.name,
            roles = EXCLUDED.roles,
            enabled = TRUE,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        RETURNING id
        """,
        "e2e-legacy-metadata-only",
        AuthManager.hash_api_key(api_key),
        ["metadata_only_e2e"],
        json.dumps(
            {
                "seed": "e2e",
                "source_roles": [
                    {
                        "source_id": e2e_config.source_id_http,
                        "role": "metadata_only_e2e",
                    }
                ],
            }
        ),
        json.dumps({"seed": "e2e"}),
    )
    await control_db.execute(
        """
        UPDATE identity_source_role_grants
        SET enabled = FALSE,
            updated_at = NOW()
        WHERE identity_id = $1 AND source_id = $2 AND role_id = $3
        """,
        identity_id,
        e2e_config.source_id_http,
        role_id,
    )

    before_denied_id = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    denied = http_proxy_request(
        e2e_config,
        "GET",
        "/json/customer",
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert denied.status_code == 403
    row = await wait_for(
        lambda: _latest_denied_for_identity(control_db, identity_id, after_id=before_denied_id)
    )
    metadata = _metadata(row)
    assert metadata["source_role_decision"]["allowed"] is False


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_removed_grant_blocks_next_request_and_audit_metadata_is_queryable(
    e2e_config,
    control_db,
    http_upstream,
) -> None:
    api_key = "ag-e2e-disable-grant"
    role_id = await _seed_role(
        control_db,
        source_id=e2e_config.source_id_http,
        role_key="disable_grant_e2e",
        permission={
            "effect": "allow",
            "action": "http.get",
            "resource_type": "http.path",
            "resource_pattern": "/json/customer",
        },
    )
    identity_id = await _seed_identity_with_grant(
        control_db,
        name="e2e-disable-grant",
        api_key=api_key,
        roles=["disable_grant_e2e"],
        source_id=e2e_config.source_id_http,
        role_id=role_id,
    )

    allowed = http_proxy_request(
        e2e_config,
        "GET",
        "/json/customer",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert allowed.status_code == 200

    await control_db.execute(
        """
        UPDATE identity_source_role_grants
        SET enabled = FALSE,
            updated_at = NOW()
        WHERE identity_id = $1 AND source_id = $2 AND role_id = $3
        """,
        identity_id,
        e2e_config.source_id_http,
        role_id,
    )
    before_denied_id = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    denied = http_proxy_request(
        e2e_config,
        "GET",
        "/json/customer",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert denied.status_code == 403

    row = await wait_for(
        lambda: _latest_denied_for_identity(control_db, identity_id, after_id=before_denied_id)
    )
    metadata = _metadata(row)
    assert metadata["source_role_decision"]["allowed"] is False
    assert await control_db.fetchval(
        """
        SELECT request_metadata ? 'source_role_decision'
        FROM audit_log
        WHERE id = $1
        """,
        row["id"],
    )
