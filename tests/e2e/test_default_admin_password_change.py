"""An admin on the default password must change it before using the console.

A fresh install bootstraps `admin` / `admin` with the change forced. The shared
e2e stack sets its own bootstrap password, so this creates a second admin in
that same state and proves the whole path against the running admin service:
sign in, find everything but the change page closed, change the password, and
find the console open, the old password dead and the change audited.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from interlock.admin.auth import hash_password

pytestmark = [pytest.mark.e2e]

NEW_PASSWORD = "e2e-changed-passphrase-" + secrets.token_hex(4)


@pytest.fixture
async def default_admin(control_db: Any) -> Any:
    username = f"e2e-default-{secrets.token_hex(4)}"
    admin_id = await control_db.fetchval(
        """
        INSERT INTO admin_identities
            (username, password_hash, roles, enabled, must_change_password)
        VALUES ($1, $2, ARRAY['owner'], TRUE, TRUE)
        RETURNING id
        """,
        username,
        hash_password("admin"),
    )
    try:
        yield username, int(admin_id)
    finally:
        await control_db.execute("DELETE FROM admin_identities WHERE id = $1", admin_id)


def _login(client: httpx.Client, username: str, password: str) -> httpx.Response:
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        headers={"Accept": "application/json"},
    )


@pytest.mark.asyncio
async def test_the_default_password_opens_only_the_change_page_until_changed(
    e2e_config: Any, control_db: Any, default_admin: tuple[str, int]
) -> None:
    username, admin_id = default_admin
    with httpx.Client(base_url=e2e_config.admin_url, follow_redirects=False, timeout=10) as c:
        signed_in = _login(c, username, "admin")
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["password_change_required"] is True
        csrf = c.get("/auth/csrf").json()["csrf"]

        page = c.get("/dashboard/overview", headers={"Accept": "text/html"})
        assert page.status_code == 302
        assert page.headers["location"] == "/auth/change-password"
        api = c.get("/api/data-sources", headers={"Accept": "application/json"})
        assert api.status_code == 403
        assert api.json() == {"error": "password_change_required"}

        refused = c.post(
            "/auth/change-password",
            data={
                "current_password": "admin",
                "new_password": "admin",
                "confirm_password": "admin",
            },
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
        )
        assert refused.status_code == 400

        changed = c.post(
            "/auth/change-password",
            data={
                "current_password": "admin",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
        )
        assert changed.status_code == 200, changed.text

        # The re-issued session now reaches the console.
        opened = c.get("/api/data-sources", headers={"Accept": "application/json"})
        assert opened.status_code == 200, opened.text

    with httpx.Client(base_url=e2e_config.admin_url, follow_redirects=False, timeout=10) as c:
        assert _login(c, username, "admin").status_code == 401
        fresh = _login(c, username, NEW_PASSWORD)
        assert fresh.status_code == 200
        assert "password_change_required" not in fresh.json()

    row = await control_db.fetchrow(
        "SELECT must_change_password, password_changed_at FROM admin_identities WHERE id = $1",
        admin_id,
    )
    assert row["must_change_password"] is False
    assert row["password_changed_at"] is not None
    audited = await control_db.fetchval(
        """
        SELECT COUNT(*) FROM admin_audit_log
        WHERE action = 'password.change' AND resource_id = $1 AND success = TRUE
        """,
        str(admin_id),
    )
    assert audited == 1
