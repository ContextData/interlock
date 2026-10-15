"""Console users: the people who sign in to the admin console.

One set of rules shared by the console pages and the JSON API, so neither can
drift from the other:

- usernames are lowercase and unique regardless of case;
- a user's roles come from a user type (Administrator, Read-write, Read-only)
  or a custom choice of the fine-grained roles;
- a new user, and a reset, gets a generated temporary password that is shown
  once and must be changed at first sign-in;
- an administrator cannot change, disable, reset or delete their own account
  here, and the last enabled administrator can never be removed;
- only a disabled account can be deleted.

Role, enabled and password changes revoke the user's sessions on their next
request: a trigger bumps ``admin_identities.authorization_version`` and the
auth middleware compares it on every request.
"""

from __future__ import annotations

import re
import secrets
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import Request

from interlock.admin.audit import audit_admin_action, mutation_audit_detail
from interlock.admin.auth import hash_password
from interlock.admin.roles import (
    CUSTOM_ROLES,
    CUSTOM_TYPE,
    USER_TYPES,
    classify_roles,
    is_console_admin,
    normalize_roles,
)

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._@-]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# No look-alike characters (0/O, 1/l/I), so a password read aloud or copied by
# hand survives the trip: 54 symbols, 20 of them, about 115 bits.
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
_PASSWORD_GROUPS = 4
_PASSWORD_GROUP_LENGTH = 5
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('interlock.admin_users'))"
_COLUMNS = (
    "id, username, roles, enabled, email, oidc_subject, created_at, updated_at, "
    "last_login_at, last_login_ip, must_change_password, password_changed_at, "
    "(password_hash IS NOT NULL) AS password_set"
)
_OTHER_ENABLED_ADMINISTRATORS_SQL = """
    SELECT count(*) FROM admin_identities
    WHERE enabled AND id <> $1
      AND EXISTS (SELECT 1 FROM unnest(roles) AS r WHERE lower(trim(r)) IN ('owner', 'admin'))
"""


class AdminUserError(Exception):
    """A request about console users that cannot be carried out, and why."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class AdminUser:
    id: int
    username: str
    roles: tuple[str, ...]
    enabled: bool
    email: str | None
    sso_subject: str | None
    created_at: datetime | None
    updated_at: datetime | None
    last_login_at: datetime | None
    last_login_ip: str | None
    must_change_password: bool
    password_changed_at: datetime | None
    password_set: bool = False

    @property
    def user_type(self) -> str:
        return classify_roles(self.roles)[0]

    @property
    def type_label(self) -> str:
        return classify_roles(self.roles)[1]

    @property
    def is_administrator(self) -> bool:
        return is_console_admin(self.roles)

    def to_public(self) -> dict[str, Any]:
        """What the API returns. Never includes the password hash."""
        return {
            "id": self.id,
            "username": self.username,
            "user_type": self.user_type,
            "type_label": self.type_label,
            "roles": list(self.roles),
            "enabled": self.enabled,
            "email": self.email,
            "sso_subject": self.sso_subject,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login_at": self.last_login_at,
            "last_login_ip": self.last_login_ip,
            "password_changed_at": self.password_changed_at,
            "password_set": self.password_set,
        }

    def audit_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "roles": list(self.roles),
            "enabled": self.enabled,
            "email": self.email,
            "sso_subject_configured": self.sso_subject is not None,
        }


def _user(row: Any) -> AdminUser:
    data = dict(row)
    return AdminUser(
        id=int(data["id"]),
        username=str(data["username"]),
        roles=tuple(data.get("roles") or ()),
        enabled=bool(data.get("enabled")),
        email=data.get("email"),
        sso_subject=data.get("oidc_subject"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        last_login_at=data.get("last_login_at"),
        last_login_ip=data.get("last_login_ip"),
        must_change_password=bool(data.get("must_change_password")),
        password_changed_at=data.get("password_changed_at"),
        password_set=bool(data.get("password_set")),
    )


# --- validation ---------------------------------------------------------------


def normalize_username(raw: str) -> str:
    username = (raw or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise AdminUserError(
            "invalid_username",
            "Usernames are 3 to 64 characters: lowercase letters, digits, and . _ @ -, "
            "starting with a letter or digit.",
        )
    return username


def resolve_roles(user_type: str, custom_roles: list[str] | tuple[str, ...] = ()) -> list[str]:
    """The roles to store for a user type, or for a custom choice of roles."""
    if user_type in USER_TYPES:
        return list(USER_TYPES[user_type][1])
    if user_type != CUSTOM_TYPE:
        raise AdminUserError("invalid_roles", f"Unknown user type: {user_type}.")
    chosen = normalize_roles(custom_roles)
    unknown = chosen - set(CUSTOM_ROLES)
    if unknown:
        raise AdminUserError("invalid_roles", "Unknown role: " + ", ".join(sorted(unknown)) + ".")
    if not chosen:
        raise AdminUserError("no_roles", "Choose at least one role for a custom user.")
    return [role for role in CUSTOM_ROLES if role in chosen]


def normalize_email(raw: str | None) -> str | None:
    email = (raw or "").strip()
    if not email:
        return None
    if len(email) > 320 or not EMAIL_RE.match(email):
        raise AdminUserError("invalid_email", "Enter a valid email address, or leave it empty.")
    return email


def normalize_sso_subject(raw: str | None) -> str | None:
    subject = raw or ""
    if not subject.strip():
        return None
    if subject != subject.strip() or len(subject) > 512 or any(ord(c) < 32 for c in subject):
        raise AdminUserError(
            "invalid_sso_subject",
            "The SSO subject cannot have surrounding spaces or control characters.",
        )
    return subject


def generate_temporary_password() -> str:
    from interlock.admin.routes.password import password_change_problem

    while True:
        groups = [
            "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_GROUP_LENGTH))
            for _ in range(_PASSWORD_GROUPS)
        ]
        password = "-".join(groups)
        if password_change_problem(username="", current="", new=password, confirm=password) is None:
            return password


# --- guards -------------------------------------------------------------------


def _assert_not_self(actor_id: int | None, user_id: int, action: str) -> None:
    if actor_id is not None and actor_id == user_id:
        raise AdminUserError(
            "self_protected",
            f"You cannot {action} your own account here. Ask another administrator, "
            "or change your password from the sidebar.",
            status=409,
        )


async def _assert_keeps_an_administrator(conn: Any, user: AdminUser) -> None:
    """Refuse to leave the console without an enabled administrator."""
    if not (user.enabled and user.is_administrator):
        return
    others = await conn.fetchval(_OTHER_ENABLED_ADMINISTRATORS_SQL, user.id)
    if not others:
        raise AdminUserError(
            "last_administrator",
            "This is the last enabled administrator. Add or enable another administrator first.",
            status=409,
        )


# --- reads ----------------------------------------------------------------------


async def list_admin_users(pool: Any) -> list[AdminUser]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {_COLUMNS} FROM admin_identities ORDER BY lower(username)")
    return [_user(row) for row in rows]


async def _fetch(conn: Any, user_id: int) -> AdminUser:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM admin_identities WHERE id = $1", user_id)
    if row is None:
        raise AdminUserError("not_found", "Console user not found.", status=404)
    return _user(row)


async def get_admin_user(pool: Any, user_id: int) -> AdminUser:
    async with pool.acquire() as conn:
        return await _fetch(conn, user_id)


class _Locked:
    """A connection in a transaction that holds the console-users lock.

    The lock serialises changes, so two administrators cannot disable each
    other at the same moment and leave none.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._acquire: Any = None
        self._tx: Any = None

    async def __aenter__(self) -> Any:
        self._acquire = self._pool.acquire()
        conn = await self._acquire.__aenter__()
        self._tx = conn.transaction() if hasattr(conn, "transaction") else nullcontext()
        await self._tx.__aenter__()
        await conn.execute(_LOCK_SQL)
        return conn

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self._tx.__aexit__(*exc)
        finally:
            await self._acquire.__aexit__(*exc)


async def _assert_unique(
    conn: Any, username: str, sso_subject: str | None, exclude_id: int | None
) -> None:
    taken = await conn.fetchval(
        "SELECT 1 FROM admin_identities WHERE lower(username) = $1 AND id IS DISTINCT FROM $2",
        username,
        exclude_id,
    )
    if taken:
        raise AdminUserError("duplicate_username", "That username is already taken.", status=409)
    if sso_subject is not None:
        mapped = await conn.fetchval(
            "SELECT 1 FROM admin_identities WHERE oidc_subject = $1 AND id IS DISTINCT FROM $2",
            sso_subject,
            exclude_id,
        )
        if mapped:
            raise AdminUserError(
                "sso_subject_taken",
                "That SSO subject already belongs to another console user.",
                status=409,
            )


def _sso_requirement(sso_enabled: bool, roles: list[str], sso_subject: str | None) -> None:
    # With SSO on, only administrators may sign in with a password (break-glass),
    # so everyone else needs an SSO subject to be able to sign in at all.
    if sso_enabled and not is_console_admin(roles) and sso_subject is None:
        raise AdminUserError(
            "sso_subject_required",
            "Single sign-on is on, so this user signs in with SSO. Enter their SSO subject.",
        )


# --- changes --------------------------------------------------------------------


async def create_admin_user(
    pool: Any,
    *,
    username: str,
    user_type: str,
    custom_roles: list[str] | tuple[str, ...] = (),
    email: str | None = None,
    sso_subject: str | None = None,
    sso_enabled: bool = False,
) -> tuple[AdminUser, str | None]:
    """Create a console user. Returns the user and its temporary password, if any."""
    name = normalize_username(username)
    roles = resolve_roles(user_type, custom_roles)
    mail = normalize_email(email)
    subject = normalize_sso_subject(sso_subject)
    _sso_requirement(sso_enabled, roles, subject)
    # With SSO on, only administrators get a password: it is their break-glass way in.
    sso_only = sso_enabled and not is_console_admin(roles)
    password = None if sso_only else generate_temporary_password()
    async with _Locked(pool) as conn:
        await _assert_unique(conn, name, subject, None)
        row = await conn.fetchrow(
            f"""
            INSERT INTO admin_identities
                (username, password_hash, roles, enabled, email, oidc_subject, must_change_password)
            VALUES ($1, $2, $3, TRUE, $4, $5, $6)
            RETURNING {_COLUMNS}
            """,
            name,
            hash_password(password) if password else None,
            roles,
            mail,
            subject,
            password is not None,
        )
    return _user(row), password


async def update_admin_user(
    pool: Any,
    *,
    actor_id: int | None,
    user_id: int,
    user_type: str,
    custom_roles: list[str] | tuple[str, ...] = (),
    email: str | None = None,
    sso_subject: str | None = None,
    sso_enabled: bool = False,
) -> tuple[AdminUser, AdminUser]:
    """Change a user's type or roles, email and SSO subject. Returns (before, after)."""
    roles = resolve_roles(user_type, custom_roles)
    mail = normalize_email(email)
    subject = normalize_sso_subject(sso_subject)
    async with _Locked(pool) as conn:
        before = await _fetch(conn, user_id)
        if normalize_roles(roles) != normalize_roles(before.roles):
            _assert_not_self(actor_id, user_id, "change the type or roles of")
            if not is_console_admin(roles):
                await _assert_keeps_an_administrator(conn, before)
        _sso_requirement(sso_enabled, roles, subject)
        await _assert_unique(conn, before.username, subject, user_id)
        row = await conn.fetchrow(
            f"""
            UPDATE admin_identities
            SET roles = $2, email = $3, oidc_subject = $4, updated_at = NOW()
            WHERE id = $1
            RETURNING {_COLUMNS}
            """,
            user_id,
            roles,
            mail,
            subject,
        )
    return before, _user(row)


async def set_enabled(
    pool: Any, *, actor_id: int | None, user_id: int, enabled: bool
) -> tuple[AdminUser, AdminUser]:
    """Disable or re-enable a user. Disabling ends their sessions at once."""
    async with _Locked(pool) as conn:
        before = await _fetch(conn, user_id)
        if not enabled:
            _assert_not_self(actor_id, user_id, "disable")
            await _assert_keeps_an_administrator(conn, before)
        row = await conn.fetchrow(
            f"""
            UPDATE admin_identities SET enabled = $2, updated_at = NOW()
            WHERE id = $1
            RETURNING {_COLUMNS}
            """,
            user_id,
            enabled,
        )
    return before, _user(row)


async def reset_password(pool: Any, *, actor_id: int | None, user_id: int) -> tuple[AdminUser, str]:
    """Give a user a new temporary password, to be changed at their next sign-in."""
    _assert_not_self(actor_id, user_id, "reset the password of")
    password = generate_temporary_password()
    async with _Locked(pool) as conn:
        await _fetch(conn, user_id)
        row = await conn.fetchrow(
            f"""
            UPDATE admin_identities
            SET password_hash = $2, must_change_password = TRUE,
                password_changed_at = NULL, updated_at = NOW()
            WHERE id = $1
            RETURNING {_COLUMNS}
            """,
            user_id,
            hash_password(password),
        )
    return _user(row), password


async def delete_admin_user(pool: Any, *, actor_id: int | None, user_id: int) -> AdminUser:
    """Delete a disabled user. Audit records keep their username."""
    _assert_not_self(actor_id, user_id, "delete")
    async with _Locked(pool) as conn:
        before = await _fetch(conn, user_id)
        if before.enabled:
            raise AdminUserError(
                "not_disabled", "Disable this user before deleting them.", status=409
            )
        await conn.execute("DELETE FROM admin_identities WHERE id = $1", user_id)
    return before


# --- audit ------------------------------------------------------------------------


def actor_id(request: Request) -> int | None:
    admin = getattr(request.state, "admin", None)
    value = getattr(admin, "admin_id", None)
    return int(value) if value is not None else None


def sso_enabled(request: Request) -> bool:
    config = getattr(request.app.state, "config", None)
    auth = getattr(config, "auth", None)
    return bool(getattr(getattr(auth, "oidc", None), "enabled", False))


async def audit_change(
    request: Request,
    *,
    action: str,
    user: AdminUser,
    before: AdminUser | None = None,
    changed_fields: tuple[str, ...] = (),
    status_code: int = 200,
) -> None:
    """Record a console-user change. Fails closed: see admin/audit.py."""
    await audit_admin_action(
        request,
        action=f"admin_user.{action}",
        resource="admin_user",
        resource_id=str(user.id),
        success=True,
        detail=mutation_audit_detail(
            before=before.audit_summary() if before else None,
            after=user.audit_summary(),
            changed_fields=changed_fields,
            status_code=status_code,
        ),
    )


def changed(before: AdminUser, after: AdminUser) -> tuple[str, ...]:
    fields = []
    if normalize_roles(before.roles) != normalize_roles(after.roles):
        fields.append("roles")
    if before.email != after.email:
        fields.append("email")
    if before.sso_subject != after.sso_subject:
        fields.append("sso_subject")
    if before.enabled != after.enabled:
        fields.append("enabled")
    return tuple(fields)
