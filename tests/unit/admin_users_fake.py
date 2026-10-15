"""An in-memory admin_identities table that answers the SQL admin_users.py sends."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

COLUMNS = (
    "id",
    "username",
    "roles",
    "enabled",
    "email",
    "oidc_subject",
    "created_at",
    "updated_at",
    "last_login_at",
    "last_login_ip",
    "must_change_password",
    "password_changed_at",
)


class FakeAdminUsersDB:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.audit: list[tuple[Any, ...]] = []
        self.locks = 0
        self._next = 1

    def add(self, username: str, roles: list[str], *, enabled: bool = True, **extra: Any) -> int:
        row_id = self._next
        self._next += 1
        now = datetime.now(UTC)
        self.rows[row_id] = {
            "id": row_id,
            "username": username,
            "roles": list(roles),
            "enabled": enabled,
            "email": None,
            "oidc_subject": None,
            "created_at": now,
            "updated_at": now,
            "last_login_at": None,
            "last_login_ip": None,
            "must_change_password": False,
            "password_changed_at": None,
            "password_hash": "scrypt$stored",
            "authorization_version": 1,
            **extra,
        }
        return row_id

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        out = {key: row[key] for key in COLUMNS}
        out["password_set"] = row.get("password_hash") is not None
        return out

    # -- pool -------------------------------------------------------------------

    def acquire(self) -> _Acquire:
        return _Acquire(_Conn(self))

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        # The auth middleware's per-request session check.
        if "SELECT enabled, authorization_version FROM admin_identities" in " ".join(sql.split()):
            row = self.rows.get(args[0])
            return (
                {"enabled": row["enabled"], "authorization_version": row["authorization_version"]}
                if row
                else None
            )
        return await _Conn(self).fetchrow(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO admin_audit_log" in sql:
            self.audit.append(args)
            return "INSERT 0 1"
        return await _Conn(self).execute(sql, *args)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Tx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, db: FakeAdminUsersDB) -> None:
        self.db = db

    def transaction(self) -> _Tx:
        return _Tx()

    async def execute(self, sql: str, *args: Any) -> str:
        if "pg_advisory_xact_lock" in sql:
            self.db.locks += 1
            return "SELECT 1"
        if sql.strip().startswith("DELETE FROM admin_identities"):
            self.db.rows.pop(args[0], None)
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM admin_identities ORDER BY lower(username)" in sql
        assert "password_hash," not in sql.replace("(password_hash IS NOT NULL)", "")
        rows = sorted(self.db.rows.values(), key=lambda r: r["username"].lower())
        return [self.db._public(r) for r in rows]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "lower(username) = $1" in sql:
            name, exclude = args
            return next(
                (
                    1
                    for r in self.db.rows.values()
                    if r["username"].lower() == name and r["id"] != exclude
                ),
                None,
            )
        if "oidc_subject = $1" in sql:
            subject, exclude = args
            return next(
                (
                    1
                    for r in self.db.rows.values()
                    if r["oidc_subject"] == subject and r["id"] != exclude
                ),
                None,
            )
        if "count(*)" in sql:
            (exclude,) = args
            return sum(
                1
                for r in self.db.rows.values()
                if r["enabled"]
                and r["id"] != exclude
                and any(role.strip().lower() in {"owner", "admin"} for role in r["roles"])
            )
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        text = " ".join(sql.split())
        if text.startswith("SELECT") and "WHERE id = $1" in text:
            row = self.db.rows.get(args[0])
            return self.db._public(row) if row else None
        if text.startswith("INSERT INTO admin_identities"):
            username, password_hash, roles, email, subject, must_change = args
            row_id = self.db.add(
                username,
                roles,
                email=email,
                oidc_subject=subject,
                must_change_password=must_change,
                password_hash=password_hash,
            )
            return self.db._public(self.db.rows[row_id])
        if text.startswith("UPDATE admin_identities"):
            row = self.db.rows.get(args[0])
            if row is None:
                return None
            watched = ("roles", "enabled", "oidc_subject", "password_hash")
            old = {key: row[key] for key in watched}
            if "SET roles = $2" in text:
                row.update(roles=list(args[1]), email=args[2], oidc_subject=args[3])
            elif "SET enabled = $2" in text:
                row["enabled"] = args[1]
            elif "SET password_hash = $2" in text:
                row.update(
                    password_hash=args[1], must_change_password=True, password_changed_at=None
                )
            else:
                raise AssertionError(f"unexpected update: {text}")
            # Migration 019's trigger: a real change ends the user's sessions.
            if any(old[key] != row[key] for key in watched):
                row["authorization_version"] += 1
            assert re.search(r"RETURNING id, username", text)
            return self.db._public(row)
        raise AssertionError(f"unexpected fetchrow: {text}")
