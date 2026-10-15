"""Console users, end to end against the running admin service.

An administrator adds a Read-only and a Read-write user through the API. Each
signs in with the one-time temporary password, is held on the change-password
page, chooses a password, and then finds the console open to exactly what
their type allows. Disabling a user ends their open session at once, and a
user can be deleted only once disabled.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.e2e]

POLICY_PROBE = {"conditions": {"tables": ["customers"]}, "actions": {"effect": "deny"}}


def _csrf(client: httpx.Client) -> str:
    response = client.get("/auth/csrf")
    response.raise_for_status()
    return str(response.json()["csrf"])


def _sign_in_and_choose_password(base_url: str, username: str, temporary: str) -> httpx.Client:
    client = httpx.Client(base_url=base_url, follow_redirects=False, timeout=10)
    login = client.post(
        "/auth/login",
        data={"username": username, "password": temporary},
        headers={"Accept": "application/json"},
    )
    assert login.status_code == 200, login.text
    assert login.json().get("password_change_required") is True

    held = client.get("/api/policies", headers={"Accept": "application/json"})
    assert held.status_code == 403 and held.json()["error"] == "password_change_required"

    chosen = "e2e-console-user-" + secrets.token_hex(6)
    change = client.post(
        "/auth/change-password",
        data={"current_password": temporary, "new_password": chosen, "confirm_password": chosen},
        headers={"X-CSRF-Token": _csrf(client), "Accept": "application/json"},
    )
    assert change.status_code == 200, change.text
    return client


@pytest.fixture
def owner(admin_session: Any) -> Iterator[tuple[httpx.Client, dict[str, str]]]:
    yield admin_session.client, {"X-CSRF-Token": admin_session.csrf_token}


def _create(
    owner: tuple[httpx.Client, dict[str, str]], user_type: str, **extra: Any
) -> dict[str, Any]:
    client, headers = owner
    username = f"e2e-{user_type.replace('_', '-')}-{secrets.token_hex(3)}"
    response = client.post(
        "/api/admin-users",
        json={"username": username, "user_type": user_type, **extra},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.headers.get("cache-control") == "no-store"
    body: dict[str, Any] = response.json()
    return body


def _cleanup(owner: tuple[httpx.Client, dict[str, str]], user_id: int) -> None:
    client, headers = owner
    client.post(f"/api/admin-users/{user_id}/disable", headers=headers)
    client.delete(f"/api/admin-users/{user_id}", headers=headers)


def test_read_only_user_reads_everything_and_changes_nothing(owner: Any, e2e_config: Any) -> None:
    body = _create(owner, "read_only")
    user = body["user"]
    try:
        assert user["type_label"] == "Read-only" and user["must_change_password"] is True
        reader = _sign_in_and_choose_password(
            e2e_config.admin_url, user["username"], body["temporary_password"]
        )

        for page in (
            "/dashboard/policies",
            "/dashboard/access-control/identities",
            "/dashboard/data-sources",
        ):
            assert reader.get(page).status_code == 200, page
        assert reader.get("/api/policies").status_code == 200

        headers = {"X-CSRF-Token": _csrf(reader), "Accept": "application/json"}
        write = reader.post("/api/policies/validate", json=POLICY_PROBE, headers=headers)
        assert write.status_code == 403
        assert reader.get("/api/admin-users").status_code == 403
        assert reader.get("/dashboard/admin-users").status_code == 403
    finally:
        _cleanup(owner, user["id"])


def test_read_write_user_changes_things_but_not_console_users(owner: Any, e2e_config: Any) -> None:
    body = _create(owner, "read_write")
    user = body["user"]
    try:
        writer = _sign_in_and_choose_password(
            e2e_config.admin_url, user["username"], body["temporary_password"]
        )
        headers = {"X-CSRF-Token": _csrf(writer), "Accept": "application/json"}

        assert (
            writer.post("/api/policies/validate", json=POLICY_PROBE, headers=headers).status_code
            == 200
        )
        assert writer.get("/api/admin-users").status_code == 403
        escalate = writer.post(
            "/api/admin-users",
            json={"username": "e2e-sneaky", "user_type": "administrator"},
            headers=headers,
        )
        assert escalate.status_code == 403
        mapping = writer.put(
            f"/api/admin-auth/oidc/admins/{user['id']}", json={"subject": "x"}, headers=headers
        )
        assert mapping.status_code == 403
    finally:
        _cleanup(owner, user["id"])


def test_disable_ends_the_session_and_delete_needs_disable_first(
    owner: Any, e2e_config: Any
) -> None:
    client, headers = owner
    body = _create(owner, "custom", roles=["auditor"])
    user = body["user"]
    try:
        auditor = _sign_in_and_choose_password(
            e2e_config.admin_url, user["username"], body["temporary_password"]
        )
        assert auditor.get("/dashboard/audit-costs").status_code == 200

        refused = client.delete(f"/api/admin-users/{user['id']}", headers=headers)
        assert refused.status_code == 409 and refused.json()["error"] == "not_disabled"

        disabled = client.post(f"/api/admin-users/{user['id']}/disable", headers=headers)
        assert disabled.status_code == 200 and disabled.json()["enabled"] is False
        after = auditor.get("/dashboard/audit-costs", headers={"Accept": "application/json"})
        assert after.status_code == 401

        assert client.delete(f"/api/admin-users/{user['id']}", headers=headers).status_code == 204
        assert client.get(f"/api/admin-users/{user['id']}").status_code == 404
    finally:
        _cleanup(owner, user["id"])


def test_reset_password_forces_a_new_choice(owner: Any, e2e_config: Any) -> None:
    client, headers = owner
    body = _create(owner, "read_only")
    user = body["user"]
    try:
        _sign_in_and_choose_password(
            e2e_config.admin_url, user["username"], body["temporary_password"]
        )
        reset = client.post(f"/api/admin-users/{user['id']}/reset-password", headers=headers)
        assert reset.status_code == 200 and reset.json()["user"]["must_change_password"] is True
        _sign_in_and_choose_password(
            e2e_config.admin_url, user["username"], reset.json()["temporary_password"]
        )
    finally:
        _cleanup(owner, user["id"])
