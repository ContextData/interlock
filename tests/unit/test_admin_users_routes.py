"""Console users through the API and the console pages, with real sessions and RBAC."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.admin.auth import create_session, sign_cookie
from interlock.config import InterLockConfig
from tests.unit.admin_users_fake import FakeAdminUsersDB
from tests.unit.test_p0_f_admin_auth import FakeRedis

SECRET = "console-users-test-secret-not-for-prod"


def _app(db: FakeAdminUsersDB, redis: FakeRedis, *, sso: bool = False) -> Any:
    config = InterLockConfig()
    config.admin.secret_key = SECRET
    config.auth.oidc.enabled = sso
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as cp,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch("interlock.admin.app.create_redis_client", new_callable=AsyncMock) as cr,
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        cp.return_value = db
        cr.return_value = redis
        app = create_app(config=config)
    app.state.pg_pool = db
    app.state.redis = redis
    app.state.admin_secret_key = SECRET
    return app


class Session:
    def __init__(self, client: AsyncClient, csrf: str) -> None:
        self.client = client
        self.csrf = csrf

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.client.get(path, **kw)

    async def send(self, method: str, path: str, *, htmx: bool = False, **kw: Any) -> Any:
        headers = {"x-csrf-token": self.csrf, **kw.pop("headers", {})}
        if htmx:
            headers["HX-Request"] = "true"
        return await self.client.request(method, path, headers=headers, **kw)


async def _session(app: Any, db: FakeAdminUsersDB, redis: FakeRedis, admin_id: int) -> Session:
    row = db.rows[admin_id]
    sid, csrf = await create_session(
        redis,
        admin_id=admin_id,
        username=row["username"],
        roles=row["roles"],
        authorization_version=row["authorization_version"],
        ttl_seconds=3600,
    )
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
        cookies={InterLockConfig().admin.cookie_name: sign_cookie({"sid": sid}, SECRET)},
    )
    return Session(client, csrf)


@pytest.fixture
def db() -> FakeAdminUsersDB:
    store = FakeAdminUsersDB()
    store.add("admin", ["owner"])  # id 1
    return store


# --- API ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_administrator_adds_a_read_only_user(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)

    resp = await owner.send(
        "POST", "/api/admin-users", json={"username": "Jane", "user_type": "read_only"}
    )

    assert resp.status_code == 201, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["user"]["username"] == "jane"
    assert body["user"]["type_label"] == "Read-only"
    assert body["user"]["roles"] == ["viewer"]
    assert body["user"]["must_change_password"] is True
    assert len(body["temporary_password"]) == 23
    assert "password_hash" not in resp.text
    audits = [args for args in db.audit if args[2] == "admin_user.create"]
    assert len(audits) == 1 and body["temporary_password"] not in str(audits)

    listed = await owner.get("/api/admin-users")
    assert [u["username"] for u in listed.json()] == ["admin", "jane"]


@pytest.mark.asyncio
@pytest.mark.parametrize("roles", [["security_admin"], ["viewer"], ["auditor"], ["policy_admin"]])
async def test_only_administrators_reach_console_users(
    db: FakeAdminUsersDB, roles: list[str]
) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    other = await _session(app, db, redis, db.add("someone", roles))

    assert (await other.get("/api/admin-users")).status_code == 403
    assert (await other.get("/dashboard/admin-users")).status_code == 403
    created = await other.send(
        "POST", "/api/admin-users", json={"username": "x1y", "user_type": "administrator"}
    )
    assert created.status_code == 403
    assert len(db.rows) == 2


@pytest.mark.asyncio
async def test_disabling_a_user_ends_their_open_session(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)
    reader_id = db.add("reader", ["viewer"])
    reader = await _session(app, db, redis, reader_id)
    assert (await reader.get("/api/catalog/search?q=x")).status_code != 401

    resp = await owner.send("POST", f"/api/admin-users/{reader_id}/disable")
    assert resp.status_code == 200 and resp.json()["enabled"] is False

    assert (await reader.get("/api/catalog/search?q=x")).status_code == 401


@pytest.mark.asyncio
async def test_delete_is_refused_until_the_user_is_disabled(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)
    target = db.add("temp", ["viewer"])

    refused = await owner.send("DELETE", f"/api/admin-users/{target}")
    assert refused.status_code == 409 and refused.json()["error"] == "not_disabled"

    await owner.send("POST", f"/api/admin-users/{target}/disable")
    assert (await owner.send("DELETE", f"/api/admin-users/{target}")).status_code == 204
    assert target not in db.rows


@pytest.mark.asyncio
async def test_an_administrator_cannot_lock_themselves_out(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)

    resp = await owner.send("POST", "/api/admin-users/1/disable")
    assert resp.status_code == 409 and resp.json()["error"] == "self_protected"
    demote = await owner.send("PUT", "/api/admin-users/1", json={"user_type": "read_only"})
    assert demote.status_code == 409
    assert db.rows[1]["enabled"] and db.rows[1]["roles"] == ["owner"]


@pytest.mark.asyncio
async def test_reset_password_returns_a_new_temporary_password_once(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)
    target = db.add("jane", ["security_admin"])

    resp = await owner.send("POST", f"/api/admin-users/{target}/reset-password")

    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    assert resp.json()["user"]["must_change_password"] is True
    assert len(resp.json()["temporary_password"]) == 23


@pytest.mark.asyncio
async def test_validation_errors_come_back_as_codes(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)

    dup = await owner.send(
        "POST", "/api/admin-users", json={"username": "ADMIN", "user_type": "read_only"}
    )
    assert dup.status_code == 409 and dup.json()["error"] == "duplicate_username"
    sneaky = await owner.send(
        "POST",
        "/api/admin-users",
        json={"username": "bob", "user_type": "custom", "roles": ["owner"]},
    )
    assert sneaky.status_code == 400 and sneaky.json()["error"] == "invalid_roles"


# --- console pages -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_page_shows_users_and_the_nav_item(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    db.add("reader", ["viewer"])
    owner = await _session(app, db, redis, 1)

    resp = await owner.get("/dashboard/admin-users")

    assert resp.status_code == 200
    assert "<h1>Console Users</h1>" in resp.text
    assert 'href="/dashboard/admin-users"' in resp.text  # the sidebar item
    assert 'data-admin-user="reader"' in resp.text and "Read-only" in resp.text


@pytest.mark.asyncio
async def test_the_form_creates_a_user_and_reveals_the_password_once(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)

    form = await owner.get("/dashboard/admin-users/new")
    assert (
        form.status_code == 200
        and 'name="user_type"' in form.text
        and 'value="custom"' in form.text
    )

    resp = await owner.send(
        "POST",
        "/dashboard/admin-users/create",
        htmx=True,
        data={"username": "rita", "user_type": "custom", "roles": ["approval_reviewer", "auditor"]},
    )
    assert resp.status_code == 200
    assert "shown" in resp.text and "once" in resp.text and "js-copy-secret" in resp.text
    assert resp.headers["cache-control"] == "no-store"
    new = next(r for r in db.rows.values() if r["username"] == "rita")
    assert new["roles"] == ["approval_reviewer", "auditor"] and new["must_change_password"]


@pytest.mark.asyncio
async def test_the_form_shows_errors_in_place(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)

    resp = await owner.send(
        "POST",
        "/dashboard/admin-users/create",
        htmx=True,
        data={"username": "x", "user_type": "read_only"},
    )
    assert resp.status_code == 200
    assert 'class="form-error"' in resp.text and "3 to 64 characters" in resp.text
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_detail_page_hides_self_destructive_actions(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)
    other = db.add("bob", ["viewer"], enabled=False)

    mine = await owner.get("/dashboard/admin-users/1")
    assert mine.status_code == 200 and "(you)" in mine.text
    assert "/reset-password" not in mine.text and "/disable" not in mine.text

    theirs = await owner.get(f"/dashboard/admin-users/{other}")
    assert "/enable" in theirs.text and "/delete" in theirs.text and "/disable" not in theirs.text

    missing = await owner.get("/dashboard/admin-users/999")
    assert missing.status_code == 200 and "Console user" in missing.text


@pytest.mark.asyncio
async def test_a_refused_page_action_explains_itself(db: FakeAdminUsersDB) -> None:
    redis = FakeRedis()
    app = _app(db, redis)
    owner = await _session(app, db, redis, 1)

    resp = await owner.send("POST", "/dashboard/admin-users/1/disable", htmx=True)

    assert resp.status_code == 200
    assert 'class="form-error"' in resp.text and "your own account" in resp.text


def test_sidebar_offers_console_users_only_to_administrators() -> None:
    config = InterLockConfig()
    config.admin.secret_key = SECRET
    template = create_app(config=config).state.templates.env.get_template("base.html")

    assert 'href="/dashboard/admin-users"' in template.render(
        is_console_admin=True, admin_username="a"
    )
    assert 'href="/dashboard/admin-users"' not in template.render(
        is_console_admin=False, admin_username="a"
    )
