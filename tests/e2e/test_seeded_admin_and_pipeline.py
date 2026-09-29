"""Admin session and shared pipeline E2E certification tests."""

from __future__ import annotations

import pytest

from tests.e2e.support.assertions import assert_audit_event
from tests.e2e.support.clients import (
    http_proxy_request,
    latest_approval,
    latest_audit,
    wait_for,
)


@pytest.mark.e2e
def test_admin_login_and_csrf_session(admin_session) -> None:
    response = admin_session.client.get("/auth/csrf")
    assert response.status_code == 200
    assert response.json()["csrf"] == admin_session.csrf_token

    approvals = admin_session.client.get("/api/approvals")
    assert approvals.status_code == 200


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_admin_approval_identity_from_session(
    e2e_config,
    control_db,
    admin_session,
    http_upstream,
) -> None:
    queued = http_proxy_request(e2e_config, "DELETE", "/mutation/session-actor")
    assert queued.status_code == 202
    approval_id = int(queued.json()["approval_id"])

    rejected = admin_session.client.post(
        f"/api/approvals/{approval_id}/reject",
        json={"rejected_by": "spoofed-user"},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert rejected.status_code == 200

    row = await latest_approval(
        control_db,
        source_id=e2e_config.source_id_http,
        status="rejected",
    )
    assert row is not None
    assert row["id"] == approval_id
    assert row["approved_by"] == "admin"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pipeline_canonical_audit_for_pg_and_http(e2e_config, control_db) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(**e2e_config.gateway_pg_params_password, timeout=10)
    try:
        await conn.fetchval("SELECT name FROM customers WHERE id = 2")
    finally:
        await conn.close()

    http_response = http_proxy_request(e2e_config, "GET", "/json/customer")
    assert http_response.status_code == 200

    pg_audit = await wait_for(
        lambda: latest_audit(
            control_db,
            source_id=e2e_config.source_id_pg,
            protocol="postgresql",
            status="success",
        )
    )
    http_audit = await wait_for(
        lambda: latest_audit(
            control_db,
            source_id=e2e_config.source_id_http,
            protocol="http",
            status="success",
        )
    )

    assert_audit_event(
        pg_audit,
        source_id=e2e_config.source_id_pg,
        protocol="postgresql",
        status="success",
    )
    assert_audit_event(
        http_audit,
        source_id=e2e_config.source_id_http,
        protocol="http",
        status="success",
    )
    assert pg_audit["identity_id"] is not None
    assert http_audit["identity_id"] is not None
