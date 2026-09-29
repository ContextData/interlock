"""Compose-backed MySQL/MariaDB connector certification tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from interlock.core.auth import AuthManager
from tests.e2e.support.clients import mcp_call, wait_for


def _mcp_text(response) -> str:
    payload = response.json()
    return str(payload["content"][0]["text"])


def _mcp_rows(response) -> list[dict[str, Any]]:
    return list(json.loads(_mcp_text(response)))


async def _seed_mysql_role(
    conn,
    *,
    source_id: str,
    role_key: str,
    permissions: list[dict[str, Any]],
) -> int:
    role_id = await conn.fetchval(
        """
        INSERT INTO source_roles
            (source_id, role_key, name, description, metadata)
        VALUES ($1, $2, initcap(replace($2, '_', ' ')),
                'E2E MySQL source role', $3::jsonb)
        ON CONFLICT (source_id, role_key) DO UPDATE
        SET name = EXCLUDED.name,
            description = EXCLUDED.description,
            enabled = TRUE,
            updated_at = NOW()
        RETURNING id
        """,
        source_id,
        role_key,
        json.dumps({"seed": "e2e", "mysql": True}),
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
            permission.get("effect", "allow"),
            permission["action"],
            permission.get("resource_type", "db.table"),
            permission.get("resource_pattern", "*.*"),
            json.dumps(permission.get("constraints") or {}),
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
                "operation_types": ["read", "write"],
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
    source_id: str,
    role_id: int,
) -> int:
    identity_id = await conn.fetchval(
        """
        INSERT INTO identities
            (name, api_key_hash, agent_type, team, roles, enabled, metadata,
             pg_username, pg_password_hash, auth_metadata)
        VALUES ($1, $2, 'codex', 'e2e', $3::text[], TRUE, $4::jsonb,
                NULL, NULL, $5::jsonb)
        ON CONFLICT (api_key_hash) DO UPDATE
        SET name = EXCLUDED.name,
            roles = EXCLUDED.roles,
            enabled = TRUE,
            metadata = EXCLUDED.metadata,
            pg_username = EXCLUDED.pg_username,
            pg_password_hash = EXCLUDED.pg_password_hash,
            auth_metadata = EXCLUDED.auth_metadata,
            updated_at = NOW()
        RETURNING id
        """,
        name,
        AuthManager.hash_api_key(api_key),
        [name],
        json.dumps({"seed": "e2e", "mysql_source_role_e2e": True}),
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
        json.dumps({"seed": "e2e", "mysql_source_role_e2e": True}),
    )
    return int(identity_id)


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


def _metadata(row) -> dict[str, Any]:
    value = row["request_metadata"]
    return _json_object(value)


def _json_object(value: Any) -> dict[str, Any]:
    for _ in range(3):
        if isinstance(value, str):
            value = json.loads(value)
            continue
        break
    return dict(value or {})


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mysql_source_seeded_with_connector_metadata(e2e_config, control_db) -> None:
    source = await control_db.fetchrow(
        """
        SELECT source_type, connection_config, metadata
        FROM data_sources
        WHERE source_id = $1
        """,
        e2e_config.source_id_mysql,
    )
    roles = await control_db.fetchval(
        "SELECT COUNT(*) FROM source_roles WHERE source_id = $1 AND enabled = TRUE",
        e2e_config.source_id_mysql,
    )

    connection_config = _json_object(source["connection_config"])
    metadata = _json_object(source["metadata"])

    assert source["source_type"] == "mysql"
    assert connection_config["host"] == e2e_config.compose_source_mysql_host
    assert metadata["connector_key"] == "mysql"
    assert roles >= 5


@pytest.mark.e2e
def test_mcp_mysql_query_uses_registered_mariadb_source(e2e_config) -> None:
    response = mcp_call(
        e2e_config,
        "agentgate_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": "SELECT name FROM customers WHERE id = 1",
        },
    )

    assert response.status_code == 200, response.text
    assert _mcp_rows(response) == [{"name": "Ada Lovelace"}]


@pytest.mark.e2e
def test_mcp_mysql_multistatement_write_is_not_executed_as_read(e2e_config) -> None:
    response = mcp_call(
        e2e_config,
        "agentgate_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": "SELECT name FROM customers WHERE id = 1; DELETE FROM customers WHERE id = 2",
        },
    )

    assert response.status_code in {202, 403}, response.text
    assert "Ada Lovelace" not in response.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mcp_mysql_source_role_limited_to_one_table(
    e2e_config,
    control_db,
) -> None:
    api_key = "ag-e2e-source-role-mysql"
    role_id = await _seed_mysql_role(
        control_db,
        source_id=e2e_config.source_id_mysql,
        role_key="mysql_customers_only_e2e",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "public.customers.*",
            }
        ],
    )
    identity_id = await _seed_identity_with_grant(
        control_db,
        name="e2e-source-role-mysql",
        api_key=api_key,
        source_id=e2e_config.source_id_mysql,
        role_id=role_id,
    )

    before_denied_id = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    allowed = mcp_call(
        e2e_config,
        "agentgate_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": "SELECT name FROM customers WHERE id = 1",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    denied = mcp_call(
        e2e_config,
        "agentgate_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": "SELECT status FROM orders WHERE id = 100",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert allowed.status_code == 200
    assert _mcp_rows(allowed) == [{"name": "Ada Lovelace"}]
    assert denied.status_code == 403

    row = await wait_for(
        lambda: _latest_denied_for_identity(control_db, identity_id, after_id=before_denied_id)
    )
    metadata = _metadata(row)
    assert metadata["source_role_decision"]["allowed"] is False
    assert "db.table.select" in json.dumps(metadata["permission_request"])


@pytest.mark.e2e
def test_mcp_mysql_blocked_identity_is_denied(e2e_config, denied_headers) -> None:
    response = mcp_call(
        e2e_config,
        "agentgate_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": "SELECT name FROM customers WHERE id = 1",
        },
        headers=denied_headers,
    )

    assert response.status_code == 403
    assert "Source role denied" in response.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mcp_mysql_delete_queues_after_role_allow(e2e_config, control_db) -> None:
    api_key = "ag-e2e-source-role-mysql-delete"
    role_id = await _seed_mysql_role(
        control_db,
        source_id=e2e_config.source_id_mysql,
        role_key="mysql_delete_e2e",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.delete_rows",
                "resource_type": "db.table",
                "resource_pattern": "public.customers.*",
            }
        ],
    )
    await _seed_identity_with_grant(
        control_db,
        name="e2e-source-role-mysql-delete",
        api_key=api_key,
        source_id=e2e_config.source_id_mysql,
        role_id=role_id,
    )

    response = mcp_call(
        e2e_config,
        "agentgate_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": "DELETE FROM customers WHERE id = 2",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert response.status_code == 202
    assert "Write queued for approval" in response.text
