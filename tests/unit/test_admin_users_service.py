"""Console users: types, validation, temporary passwords and the guards."""

from __future__ import annotations

import pytest

from interlock.admin import admin_users as users
from interlock.admin.auth import verify_password
from interlock.admin.routes.password import password_change_problem
from tests.unit.admin_users_fake import FakeAdminUsersDB


@pytest.fixture
def db() -> FakeAdminUsersDB:
    store = FakeAdminUsersDB()
    store.add("admin", ["owner"])  # id 1, the signed-in administrator in these tests
    return store


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize("raw", ["Jane.Doe", " jane@example.com ", "ops-1", "a_b"])
def test_usernames_are_lowercased(raw: str) -> None:
    assert users.normalize_username(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw", ["", "ab", "-jane", "jane doe", "jané", "x" * 65, "jane;drop"])
def test_bad_usernames_are_refused(raw: str) -> None:
    with pytest.raises(users.AdminUserError) as err:
        users.normalize_username(raw)
    assert err.value.code == "invalid_username"


@pytest.mark.parametrize(
    ("user_type", "custom", "roles"),
    [
        ("administrator", [], ["owner"]),
        ("read_write", [], ["security_admin"]),
        ("read_only", [], ["viewer"]),
        ("custom", ["auditor", "approval_reviewer"], ["approval_reviewer", "auditor"]),
        ("read_only", ["owner"], ["viewer"]),
    ],
)
def test_user_types_map_to_roles(user_type: str, custom: list[str], roles: list[str]) -> None:
    assert users.resolve_roles(user_type, custom) == roles


@pytest.mark.parametrize(
    ("user_type", "custom", "code"),
    [
        ("custom", [], "no_roles"),
        ("custom", ["owner"], "invalid_roles"),
        ("custom", ["admin"], "invalid_roles"),
        ("custom", ["superuser"], "invalid_roles"),
        ("root", [], "invalid_roles"),
    ],
)
def test_custom_roles_cannot_reach_administrator(
    user_type: str, custom: list[str], code: str
) -> None:
    with pytest.raises(users.AdminUserError) as err:
        users.resolve_roles(user_type, custom)
    assert err.value.code == code


def test_temporary_passwords_meet_the_policy_and_differ() -> None:
    seen = set()
    for _ in range(200):
        password = users.generate_temporary_password()
        assert (
            password_change_problem(username="u", current="", new=password, confirm=password)
            is None
        )
        assert len(password) == 23 and password.count("-") == 3
        assert not set(password) & set("0O1lI")
        seen.add(password)
    assert len(seen) == 200


# --- create -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_gives_a_temporary_password_that_must_be_changed(db: FakeAdminUsersDB) -> None:
    user, password = await users.create_admin_user(db, username="Jane", user_type="read_only")

    assert user.username == "jane" and user.roles == ("viewer",) and user.enabled
    assert user.must_change_password and user.password_set
    stored = db.rows[user.id]["password_hash"]
    assert password and verify_password(password, stored)
    assert password not in str(user.to_public())
    assert "password_hash" not in user.to_public()
    assert db.locks == 1


@pytest.mark.asyncio
async def test_usernames_are_unique_regardless_of_case(db: FakeAdminUsersDB) -> None:
    db.add("Legacy.User", ["viewer"])
    with pytest.raises(users.AdminUserError) as err:
        await users.create_admin_user(db, username="legacy.user", user_type="read_only")
    assert err.value.code == "duplicate_username" and err.value.status == 409


@pytest.mark.asyncio
async def test_with_sso_on_non_administrators_need_a_subject_and_get_no_password(
    db: FakeAdminUsersDB,
) -> None:
    with pytest.raises(users.AdminUserError) as err:
        await users.create_admin_user(db, username="sam", user_type="read_only", sso_enabled=True)
    assert err.value.code == "sso_subject_required"

    user, password = await users.create_admin_user(
        db, username="sam", user_type="read_only", sso_subject="00u1abc", sso_enabled=True
    )
    assert password is None and not user.password_set and not user.must_change_password

    admin, admin_password = await users.create_admin_user(
        db, username="breakglass", user_type="administrator", sso_enabled=True
    )
    assert admin_password and admin.must_change_password


@pytest.mark.asyncio
async def test_an_sso_subject_belongs_to_one_console_user(db: FakeAdminUsersDB) -> None:
    db.add("first", ["viewer"], oidc_subject="00u1")
    with pytest.raises(users.AdminUserError) as err:
        await users.create_admin_user(
            db, username="second", user_type="read_only", sso_subject="00u1"
        )
    assert err.value.code == "sso_subject_taken"


# --- guards -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_administrator_cannot_change_their_own_account_here(db: FakeAdminUsersDB) -> None:
    db.add("other", ["owner"])
    for call in (
        users.update_admin_user(db, actor_id=1, user_id=1, user_type="read_only"),
        users.set_enabled(db, actor_id=1, user_id=1, enabled=False),
        users.reset_password(db, actor_id=1, user_id=1),
        users.delete_admin_user(db, actor_id=1, user_id=1),
    ):
        with pytest.raises(users.AdminUserError) as err:
            await call
        assert err.value.code == "self_protected"
    assert db.rows[1]["roles"] == ["owner"] and db.rows[1]["enabled"]


@pytest.mark.asyncio
async def test_saving_your_own_email_is_allowed(db: FakeAdminUsersDB) -> None:
    _, after = await users.update_admin_user(
        db, actor_id=1, user_id=1, user_type="administrator", email="me@example.com"
    )
    assert after.email == "me@example.com" and after.roles == ("owner",)


@pytest.mark.asyncio
async def test_the_last_enabled_administrator_cannot_be_removed(db: FakeAdminUsersDB) -> None:
    # No acting session id here, so the self guard cannot mask this one.
    with pytest.raises(users.AdminUserError) as err:
        await users.set_enabled(db, actor_id=None, user_id=1, enabled=False)
    assert err.value.code == "last_administrator"
    with pytest.raises(users.AdminUserError) as err:
        await users.update_admin_user(db, actor_id=None, user_id=1, user_type="read_write")
    assert err.value.code == "last_administrator"

    other = db.add("second", ["Owner"])  # mixed-case legacy role still counts
    await users.set_enabled(db, actor_id=other, user_id=1, enabled=False)
    assert not db.rows[1]["enabled"]


@pytest.mark.asyncio
async def test_a_disabled_administrator_does_not_count(db: FakeAdminUsersDB) -> None:
    db.add("dormant", ["owner"], enabled=False)
    with pytest.raises(users.AdminUserError) as err:
        await users.update_admin_user(
            db, actor_id=None, user_id=1, user_type="custom", custom_roles=["auditor"]
        )
    assert err.value.code == "last_administrator"


@pytest.mark.asyncio
async def test_delete_only_after_disable(db: FakeAdminUsersDB) -> None:
    user, _ = await users.create_admin_user(db, username="temp", user_type="read_only")
    with pytest.raises(users.AdminUserError) as err:
        await users.delete_admin_user(db, actor_id=1, user_id=user.id)
    assert err.value.code == "not_disabled" and err.value.status == 409

    await users.set_enabled(db, actor_id=1, user_id=user.id, enabled=False)
    gone = await users.delete_admin_user(db, actor_id=1, user_id=user.id)
    assert gone.username == "temp" and user.id not in db.rows


@pytest.mark.asyncio
async def test_reset_password_forces_a_change_and_replaces_the_hash(db: FakeAdminUsersDB) -> None:
    target = db.add("jane", ["viewer"], password_changed_at="earlier")
    old = db.rows[target]["password_hash"]
    user, password = await users.reset_password(db, actor_id=1, user_id=target)

    assert user.must_change_password and user.password_changed_at is None
    assert db.rows[target]["password_hash"] != old
    assert verify_password(password, db.rows[target]["password_hash"])


@pytest.mark.asyncio
async def test_unknown_user_is_not_found(db: FakeAdminUsersDB) -> None:
    with pytest.raises(users.AdminUserError) as err:
        await users.get_admin_user(db, 999)
    assert err.value.status == 404


def test_audit_summary_never_carries_secrets() -> None:
    user = users.AdminUser(
        id=7,
        username="jane",
        roles=("viewer",),
        enabled=True,
        email="jane@example.com",
        sso_subject="00u1",
        created_at=None,
        updated_at=None,
        last_login_at=None,
        last_login_ip=None,
        must_change_password=True,
        password_changed_at=None,
        password_set=True,
    )
    summary = user.audit_summary()
    assert summary["sso_subject_configured"] is True
    assert "00u1" not in str(summary)
    assert not {key for key in summary if "password" in key or "hash" in key}


def test_console_user_changes_are_audited_strictly() -> None:
    from interlock.admin.audit import _STRICT_MUTATION_PREFIXES

    assert "admin_user." in _STRICT_MUTATION_PREFIXES
