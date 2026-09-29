"""Seeded HTTP proxy and approval E2E certification tests."""

from __future__ import annotations

import pytest

from tests.e2e.support.assertions import (
    assert_approval_state,
    assert_audit_event,
    assert_interlock_auth_not_forwarded,
)
from tests.e2e.support.clients import (
    http_proxy_request,
    latest_approval,
    latest_audit,
    upstream_calls,
    wait_for,
)


@pytest.mark.e2e
def test_http_get_uses_registered_source_and_strips_interlock_auth(
    e2e_config,
    http_upstream,
) -> None:
    response = http_proxy_request(e2e_config, "GET", "/json/customer")

    assert response.status_code == 200
    assert response.json()["name"] == "Ada Lovelace"
    assert_interlock_auth_not_forwarded(upstream_calls(e2e_config))


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_get_cache_redaction_and_audit(
    e2e_config,
    control_db,
    http_upstream,
) -> None:
    first = http_proxy_request(e2e_config, "GET", "/json/customer")
    second = http_proxy_request(e2e_config, "GET", "/json/customer")

    assert first.status_code == 200
    assert second.status_code == 200
    assert "123-45-6789" not in first.text
    assert "123-45-6789" not in second.text
    assert len(upstream_calls(e2e_config)["calls"]) == 1

    row = await wait_for(
        lambda: latest_audit(
            control_db,
            source_id=e2e_config.source_id_http,
            protocol="http",
            status="success",
        )
    )
    assert_audit_event(
        row,
        source_id=e2e_config.source_id_http,
        protocol="http",
        status="success",
    )


@pytest.mark.e2e
def test_http_text_and_csv_redaction(e2e_config, http_upstream) -> None:
    text_response = http_proxy_request(e2e_config, "GET", "/text/customer")
    csv_response = http_proxy_request(e2e_config, "GET", "/csv/customers")

    assert text_response.status_code == 200
    assert csv_response.status_code == 200
    assert "123-45-6789" not in text_response.text
    assert "123-45-6789" not in csv_response.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_delete_queues_approval_without_upstream_call(
    e2e_config,
    control_db,
    http_upstream,
) -> None:
    response = http_proxy_request(e2e_config, "DELETE", "/mutation/1")

    assert response.status_code == 202
    assert response.json()["approval_state"] == "pending"
    assert upstream_calls(e2e_config)["mutations"] == []

    approval = await wait_for(
        lambda: latest_approval(
            control_db,
            source_id=e2e_config.source_id_http,
            status="pending",
        )
    )
    assert_approval_state(
        approval,
        source_id=e2e_config.source_id_http,
        status="pending",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_admin_reject_does_not_execute(
    e2e_config,
    control_db,
    admin_session,
    http_upstream,
) -> None:
    queued = http_proxy_request(e2e_config, "DELETE", "/mutation/2")
    assert queued.status_code == 202
    approval_id = int(queued.json()["approval_id"])

    rejected = admin_session.client.post(
        f"/api/approvals/{approval_id}/reject",
        json={},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert upstream_calls(e2e_config)["mutations"] == []

    approval = await latest_approval(
        control_db,
        source_id=e2e_config.source_id_http,
        status="rejected",
    )
    assert_approval_state(
        approval,
        source_id=e2e_config.source_id_http,
        status="rejected",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_delete_approval_executes_once(
    e2e_config,
    control_db,
    admin_session,
    http_upstream,
) -> None:
    queued = http_proxy_request(e2e_config, "DELETE", "/mutation/approve-once")
    assert queued.status_code == 202
    approval_id = int(queued.json()["approval_id"])

    approved = admin_session.client.post(
        f"/api/approvals/{approval_id}/approve",
        json={},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "executed"
    assert approved.json()["executed"] is True

    duplicate = admin_session.client.post(
        f"/api/approvals/{approval_id}/approve",
        json={},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert duplicate.status_code == 409

    mutations = [
        mutation
        for mutation in upstream_calls(e2e_config)["mutations"]
        if mutation["path"] == "/mutation/approve-once"
    ]
    assert len(mutations) == 1

    approval = await latest_approval(
        control_db,
        source_id=e2e_config.source_id_http,
        status="executed",
    )
    assert_approval_state(
        approval,
        source_id=e2e_config.source_id_http,
        status="executed",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_expired_approval_never_executes(
    e2e_config,
    control_db,
    admin_session,
    http_upstream,
) -> None:
    queued = http_proxy_request(e2e_config, "DELETE", "/mutation/expired")
    assert queued.status_code == 202
    approval_id = int(queued.json()["approval_id"])
    await control_db.execute(
        "UPDATE write_approval_queue SET expires_at = NOW() - INTERVAL '1 second' WHERE id = $1",
        approval_id,
    )

    approved = admin_session.client.post(
        f"/api/approvals/{approval_id}/approve",
        json={},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )

    assert approved.status_code == 409
    mutations = [
        mutation
        for mutation in upstream_calls(e2e_config)["mutations"]
        if mutation["path"] == "/mutation/expired"
    ]
    assert mutations == []
    approval = await latest_approval(
        control_db,
        source_id=e2e_config.source_id_http,
        status="expired",
    )
    assert_approval_state(
        approval,
        source_id=e2e_config.source_id_http,
        status="expired",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_policy_denial_is_audited(e2e_config, control_db, denied_headers) -> None:
    response = http_proxy_request(
        e2e_config,
        "GET",
        "/json/customer",
        headers=denied_headers,
    )

    assert response.status_code == 403
    row = await wait_for(
        lambda: latest_audit(
            control_db,
            source_id=e2e_config.source_id_http,
            protocol="http",
            status="denied",
        )
    )
    assert_audit_event(
        row,
        source_id=e2e_config.source_id_http,
        protocol="http",
        status="denied",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_missing_auth_is_audited_or_rejected_consistently(
    e2e_config,
    control_db,
    http_upstream,
) -> None:
    response = http_proxy_request(e2e_config, "GET", "/json/customer", headers={})

    assert response.status_code == 401
    row = await wait_for(
        lambda: latest_audit(
            control_db,
            source_id=e2e_config.source_id_http,
            protocol="http",
            status="error",
        )
    )
    assert_audit_event(
        row,
        source_id=e2e_config.source_id_http,
        protocol="http",
        status="error",
    )
