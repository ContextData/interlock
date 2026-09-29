"""A fresh install signs in as admin / admin and must change the password first.

The quick start never produced a login: the admin was created only when an
operator exported a bootstrap password, which a first-time user has no way to
know. Now the first admin is `admin` with the documented default password
`admin`, and that account can reach nothing but the change-password page until
the password is changed. An operator-supplied bootstrap password is trusted as
chosen and not forced.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import _bootstrap_admin_if_needed, create_app
from interlock.admin.auth import (
    SESSION_REDIS_PREFIX,
    create_session,
    hash_password,
    sign_cookie,
    verify_password,
)
from interlock.admin.routes.password import password_change_problem
from interlock.config import InterLockConfig
from tests.unit.test_p0_f_admin_auth import FakeRedis

SECRET = "test-secret-key-not-for-prod"
NEW_PASSWORD = "a-much-better-passphrase"


class _Conn:
    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def fetch(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def fetchrow(self, *a: Any, **k: Any) -> Any:
        return None

    async def fetchval(self, *a: Any, **k: Any) -> Any:
        return 0

    async def execute(self, *a: Any, **k: Any) -> str:
        return "OK"


class AdminStore:
    """One admin row, with the migration 019 trigger's version bump."""

    def __init__(self, password: str = "admin", must_change: bool = True) -> None:
        self.admin: dict[str, Any] = {
            "id": 1,
            "username": "admin",
            "password_hash": hash_password(password),
            "roles": ["owner"],
            "enabled": True,
            "authorization_version": 1,
            "must_change_password": must_change,
        }
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self) -> _Conn:
        return _Conn()

    async def fetch(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        return 0

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "admin_identities" not in query:
            return None
        if query.lstrip().startswith("UPDATE admin_identities") and "password_hash = $2" in query:
            self.admin["password_hash"] = args[1]
            self.admin["must_change_password"] = False
            self.admin["authorization_version"] += 1
            return dict(self.admin)
        if "WHERE username = $1" in query:
            return dict(self.admin) if args[0] == self.admin["username"] else None
        return dict(self.admin)

    def audits(self, action: str) -> list[tuple[Any, ...]]:
        return [a for q, a in self.executed if "admin_audit_log" in q and a[2] == action]


def _app(store: AdminStore, redis: FakeRedis) -> Any:
    config = InterLockConfig()
    config.admin.secret_key = SECRET
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as cp,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch("interlock.admin.app.create_redis_client", new_callable=AsyncMock) as cr,
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        cp.return_value = store
        cr.return_value = redis
        app = create_app(config=config)
    app.state.pg_pool = store
    app.state.redis = redis
    app.state.admin_secret_key = SECRET
    return app


async def _signed_in(
    store: AdminStore, redis: FakeRedis, *, must_change: bool = True
) -> tuple[str, str, dict[str, str]]:
    sid, csrf = await create_session(
        redis,
        admin_id=1,
        username="admin",
        roles=["owner"],
        authorization_version=store.admin["authorization_version"],
        ttl_seconds=3600,
        must_change_password=must_change,
    )
    config = InterLockConfig()
    cookies = {config.admin.cookie_name: sign_cookie({"sid": sid}, SECRET)}
    return sid, csrf, cookies


def _client(app: Any, cookies: dict[str, str] | None = None) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
        cookies=cookies or {},
    )


# --- bootstrap ---------------------------------------------------------------


class _BootstrapPool:
    def __init__(self, existing: int = 0) -> None:
        self.existing = existing
        self.inserts: list[tuple[Any, ...]] = []

    async def fetchval(self, query: str, *args: Any) -> int:
        return self.existing

    async def execute(self, query: str, *args: Any) -> str:
        self.inserts.append(args)
        return "INSERT 0 1"


@pytest.mark.anyio
async def test_a_fresh_install_bootstraps_admin_admin_that_must_change() -> None:
    pool = _BootstrapPool()
    await _bootstrap_admin_if_needed(pool, InterLockConfig())

    [(username, pwhash, roles, must_change)] = pool.inserts
    assert username == "admin"
    assert verify_password("admin", pwhash)
    assert roles == ["owner"]
    assert must_change is True


@pytest.mark.anyio
async def test_an_operator_supplied_bootstrap_password_is_not_forced() -> None:
    config = InterLockConfig()
    config.admin.bootstrap_password = "operator-chosen-passphrase"
    pool = _BootstrapPool()
    await _bootstrap_admin_if_needed(pool, config)

    [(_, pwhash, _, must_change)] = pool.inserts
    assert verify_password("operator-chosen-passphrase", pwhash)
    assert must_change is False


@pytest.mark.anyio
async def test_bootstrap_leaves_an_existing_install_alone() -> None:
    pool = _BootstrapPool(existing=1)
    await _bootstrap_admin_if_needed(pool, InterLockConfig())
    assert pool.inserts == []


# --- login ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_with_the_default_password_lands_on_the_change_page() -> None:
    store, redis = AdminStore(), FakeRedis()
    async with _client(_app(store, redis)) as c:
        html = await c.post(
            "/auth/login",
            data={"username": "admin", "password": "admin"},
            headers={"accept": "text/html"},
        )
        api = await c.post("/auth/login", data={"username": "admin", "password": "admin"})
    assert html.status_code == 302
    assert html.headers["location"] == "/auth/change-password"
    assert api.json()["password_change_required"] is True


@pytest.mark.anyio
async def test_login_without_the_flag_lands_on_the_overview() -> None:
    store, redis = AdminStore(password="chosen-passphrase", must_change=False), FakeRedis()
    async with _client(_app(store, redis)) as c:
        html = await c.post(
            "/auth/login",
            data={"username": "admin", "password": "chosen-passphrase"},
            headers={"accept": "text/html"},
        )
    assert html.headers["location"] == "/dashboard/overview"


# --- the gate ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_gate_sends_every_kind_of_request_to_the_change_page() -> None:
    store, redis = AdminStore(), FakeRedis()
    _, csrf, cookies = await _signed_in(store, redis)
    async with _client(_app(store, redis), cookies) as c:
        page = await c.get("/dashboard/overview", headers={"accept": "text/html"})
        htmx = await c.get("/dashboard/data-sources", headers={"hx-request": "true"})
        api = await c.get("/api/data-sources", headers={"accept": "application/json"})
        write = await c.post(
            "/api/policies", json={}, headers={"x-csrf-token": csrf, "accept": "application/json"}
        )
        allowed = await c.get("/auth/change-password", headers={"accept": "text/html"})

    assert page.status_code == 302 and page.headers["location"] == "/auth/change-password"
    assert htmx.status_code == 200 and htmx.headers["hx-redirect"] == "/auth/change-password"
    assert api.status_code == 403 and api.json() == {"error": "password_change_required"}
    assert write.status_code == 403 and write.json() == {"error": "password_change_required"}
    assert allowed.status_code == 200
    assert "Choose a new password to continue" in allowed.text


@pytest.mark.anyio
async def test_logout_stays_reachable_before_the_change() -> None:
    store, redis = AdminStore(), FakeRedis()
    _, csrf, cookies = await _signed_in(store, redis)
    async with _client(_app(store, redis), cookies) as c:
        r = await c.post("/auth/logout", headers={"x-csrf-token": csrf})
    assert r.status_code == 302 and r.headers["location"] == "/auth/login"


@pytest.mark.anyio
async def test_an_admin_without_the_flag_is_not_gated() -> None:
    store, redis = AdminStore(must_change=False), FakeRedis()
    _, _, cookies = await _signed_in(store, redis, must_change=False)
    async with _client(_app(store, redis), cookies) as c:
        r = await c.get("/auth/change-password", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "Choose a new password to continue" not in r.text
    assert 'href="/auth/change-password"' in r.text  # the sidebar link


# --- the change ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "confirm", "expected"),
    [
        ("short", "short", "at least 12 characters"),
        ("long-enough-pass", "long-enough-typo", "do not match"),
        ("admin-username", "admin-username", None),
        ("current-password-1", "current-password-1", "differ from the current"),
        (NEW_PASSWORD, NEW_PASSWORD, None),
    ],
)
def test_password_rules(new: str, confirm: str, expected: str | None) -> None:
    problem = password_change_problem(
        username="admin", current="current-password-1", new=new, confirm=confirm
    )
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem


def test_the_default_password_and_the_username_are_refused_at_any_length() -> None:
    long_name = "operator-with-a-long-name"
    assert "default password or your username" in (
        password_change_problem(username=long_name, current="x", new=long_name, confirm=long_name)
        or ""
    )


@pytest.mark.anyio
async def test_a_change_clears_the_flag_reissues_the_session_and_is_audited() -> None:
    store, redis = AdminStore(), FakeRedis()
    old_sid, csrf, cookies = await _signed_in(store, redis)
    async with _client(_app(store, redis), cookies) as c:
        r = await c.post(
            "/auth/change-password",
            data={
                "current_password": "admin",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
            headers={"x-csrf-token": csrf, "hx-request": "true"},
        )
        assert r.status_code == 200
        assert r.headers["hx-redirect"] == "/dashboard/overview"
        # The re-issued session reaches the console with the bumped version.
        after = await c.get("/auth/change-password", headers={"accept": "text/html"})

    assert verify_password(NEW_PASSWORD, store.admin["password_hash"])
    assert store.admin["must_change_password"] is False
    assert store.admin["authorization_version"] == 2
    assert SESSION_REDIS_PREFIX + old_sid not in redis.store
    assert after.status_code == 200 and "Choose a new password" not in after.text
    [audit] = [a for a in store.audits("password.change") if a[8] is True]
    assert audit[4] == "1"


@pytest.mark.anyio
async def test_a_wrong_current_password_changes_nothing_and_is_throttled() -> None:
    store, redis = AdminStore(), FakeRedis()
    _, csrf, cookies = await _signed_in(store, redis)
    form = {
        "current_password": "wrong",
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }
    async with _client(_app(store, redis), cookies) as c:
        first = await c.post("/auth/change-password", data=form, headers={"x-csrf-token": csrf})
        for _ in range(5):
            last = await c.post("/auth/change-password", data=form, headers={"x-csrf-token": csrf})

    assert first.status_code == 400
    assert first.json() == {"error": "The current password is incorrect."}
    assert last.status_code == 429
    assert verify_password("admin", store.admin["password_hash"])
    assert store.admin["must_change_password"] is True


@pytest.mark.anyio
async def test_a_weak_new_password_is_refused_in_the_form() -> None:
    store, redis = AdminStore(), FakeRedis()
    _, csrf, cookies = await _signed_in(store, redis)
    async with _client(_app(store, redis), cookies) as c:
        r = await c.post(
            "/auth/change-password",
            data={
                "current_password": "admin",
                "new_password": "admin",
                "confirm_password": "admin",
            },
            headers={"x-csrf-token": csrf, "hx-request": "true"},
        )
    assert r.status_code == 200
    assert 'id="change-password-form"' in r.text
    assert "at least 12 characters" in r.text
    assert store.admin["must_change_password"] is True
