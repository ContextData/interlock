"""A source disabled in the console stops answering at once, cache included.

The readiness assessment reproduced this against a live deployment: after a
source was disabled, an MCP agent still got rows for any statement cached while
it was enabled, and only a new statement was refused. This is that
reproduction, on every protocol: cache a PostgreSQL statement over MCP and an
HTTP response over the proxy, keep a PG-wire session open, disable both sources
through the console, and require all three to be refused. Re-enabling must
bring them back.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import asyncpg
import httpx
import pytest

from tests.e2e.support.clients import mcp_call, wait_for

pytestmark = [pytest.mark.e2e]

_SQL = "SELECT 1 AS disabled_source_probe"


async def _notify(control_db: Any) -> None:
    await control_db.execute("NOTIFY onyx_config_changed")
    await control_db.execute(
        "SELECT pg_notify('onyx_policy_changed', $1)",
        json.dumps({"test": "disabled_source_serves_nothing"}),
    )


async def _clone_source(control_db: Any, template_id: str, source_id: str, role: str) -> None:
    """A cached copy of a seeded source, granted to the e2e agent and allowed by policy."""
    template = await control_db.fetchrow(
        "SELECT source_type, connection_config, metadata FROM data_sources WHERE source_id = $1",
        template_id,
    )
    assert template is not None, f"the seeded source {template_id} is missing"
    await control_db.execute(
        """
        INSERT INTO data_sources
            (source_id, name, source_type, connection_config, cache_strategy, enabled, metadata)
        VALUES ($1, $1, $2, $3::jsonb, 'deterministic_first', TRUE, $4::jsonb)
        """,
        source_id,
        template["source_type"],
        json.dumps(_as_dict(template["connection_config"])),
        json.dumps(_as_dict(template["metadata"])),
    )
    template_role = await control_db.fetchval(
        "SELECT id FROM source_roles WHERE source_id = $1 AND role_key = $2", template_id, role
    )
    role_id = await control_db.fetchval(
        """
        INSERT INTO source_roles (source_id, role_key, name, description, review_required, metadata)
        VALUES ($1, $2, $2, 'e2e disabled-source role', FALSE, '{}'::jsonb)
        RETURNING id
        """,
        source_id,
        role,
    )
    await control_db.execute(
        """
        INSERT INTO source_role_permissions
            (role_id, effect, action, resource_type, resource_pattern, constraints)
        SELECT $1, effect, action, resource_type, resource_pattern, constraints
        FROM source_role_permissions WHERE role_id = $2
        """,
        role_id,
        template_role,
    )
    agent_id = await control_db.fetchval("SELECT id FROM identities WHERE name = 'e2e-agent'")
    await control_db.execute(
        """
        INSERT INTO identity_source_role_grants (identity_id, source_id, role_id, metadata)
        VALUES ($1, $2, $3, '{}'::jsonb)
        """,
        agent_id,
        source_id,
        role_id,
    )
    await control_db.execute(
        """
        INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
        VALUES ($1, 10, $2::jsonb, '{"effect": "allow"}'::jsonb, TRUE)
        """,
        f"e2e-allow-{source_id}",
        json.dumps({"source_ids": [source_id], "operation_types": ["read"]}),
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})


async def _drop_source(control_db: Any, source_id: str) -> None:
    await control_db.execute(
        "UPDATE data_sources SET enabled = FALSE WHERE source_id = $1", source_id
    )
    await control_db.execute("DELETE FROM policy_rules WHERE name = $1", f"e2e-allow-{source_id}")
    await control_db.execute(
        "DELETE FROM identity_source_role_grants WHERE source_id = $1", source_id
    )
    await control_db.execute(
        "DELETE FROM source_role_permissions WHERE role_id IN "
        "(SELECT id FROM source_roles WHERE source_id = $1)",
        source_id,
    )
    await control_db.execute("DELETE FROM source_roles WHERE source_id = $1", source_id)
    await control_db.execute("DELETE FROM data_sources WHERE source_id = $1", source_id)


@pytest.fixture
async def scratch_sources(e2e_config: Any, control_db: Any) -> Any:
    suffix = secrets.token_hex(4)
    pg_id, http_id = f"e2e_disable_pg_{suffix}", f"e2e_disable_http_{suffix}"
    try:
        await _clone_source(control_db, e2e_config.source_id_pg, pg_id, "read")
        await _clone_source(control_db, e2e_config.source_id_http, http_id, "read")
        await _notify(control_db)
        yield pg_id, http_id
    finally:
        # Disabled first: a leaked enabled source takes traffic from other suites.
        for source_id in (pg_id, http_id):
            await _drop_source(control_db, source_id)
        await _notify(control_db)


def _proxy_get(e2e_config: Any, source_id: str) -> httpx.Response:
    with httpx.Client(base_url=e2e_config.gateway_url, timeout=15) as client:
        return client.get(
            f"/proxy/{source_id}/json/customer",
            headers={"Authorization": f"Bearer {e2e_config.agent_api_key}"},
        )


async def _cache_hits(control_db: Any, source_id: str) -> int:
    return int(
        await control_db.fetchval(
            "SELECT COUNT(*) FROM audit_log WHERE source_id = $1 AND cache_hit = TRUE", source_id
        )
    )


def _toggle(admin_session: Any, source_id: str) -> None:
    response = admin_session.client.post(
        f"/dashboard/data-sources/{source_id}/toggle-enabled",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert response.status_code in (200, 303), response.text[:300]


@pytest.mark.asyncio
async def test_a_disabled_source_serves_nothing_from_cache_or_upstream(
    e2e_config: Any, control_db: Any, admin_session: Any, scratch_sources: tuple[str, str]
) -> None:
    pg_id, http_id = scratch_sources
    query = {"source_id": pg_id, "sql": _SQL}

    async def mcp_ok() -> bool:
        return mcp_call(e2e_config, "interlock_query", query).status_code == 200

    async def http_ok() -> bool:
        return _proxy_get(e2e_config, http_id).status_code == 200

    assert await wait_for(mcp_ok, timeout_seconds=20), "the scratch PG source never served"
    assert await wait_for(http_ok, timeout_seconds=20), "the scratch HTTP source never served"

    # Prove both answers are now cached: a repeat is recorded as a cache hit.
    async def cached() -> bool:
        mcp_call(e2e_config, "interlock_query", query)
        _proxy_get(e2e_config, http_id)
        return (
            await _cache_hits(control_db, pg_id) > 0 and await _cache_hits(control_db, http_id) > 0
        )

    assert await wait_for(cached, timeout_seconds=20), "the statements were never served from cache"

    session = await asyncpg.connect(
        **{**e2e_config.gateway_pg_params_api_key, "database": pg_id}, timeout=10
    )
    try:
        assert await session.fetchval(_SQL) == 1

        _toggle(admin_session, pg_id)
        _toggle(admin_session, http_id)

        async def mcp_refused() -> httpx.Response | None:
            response = mcp_call(e2e_config, "interlock_query", query)
            return response if response.status_code == 404 else None

        refused = await wait_for(mcp_refused, timeout_seconds=20)
        assert refused is not None, "MCP kept answering a disabled source"
        assert f"Unknown data source: {pg_id}" in refused.text

        async def http_refused() -> bool:
            return _proxy_get(e2e_config, http_id).status_code == 404

        assert await wait_for(http_refused, timeout_seconds=20), "HTTP kept answering"

        with pytest.raises(asyncpg.PostgresError) as caught:
            await session.fetchval(_SQL)
        assert caught.value.sqlstate == "3D000"
        assert f"Unknown data source: {pg_id}" in str(caught.value)
    finally:
        if not session.is_closed():
            session.terminate()

    # Re-enabling brings the source back.
    _toggle(admin_session, pg_id)
    _toggle(admin_session, http_id)
    assert await wait_for(mcp_ok, timeout_seconds=20), "re-enabled PG source did not answer"
    assert await wait_for(http_ok, timeout_seconds=20), "re-enabled HTTP source did not answer"
