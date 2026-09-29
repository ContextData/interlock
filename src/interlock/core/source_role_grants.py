"""Granting and revoking an identity's source roles.

Grants used to be writable in exactly one place - the identity *create*
handler - so the only way to give an existing agent access to a newly
registered source was to delete the identity and recreate it, which mints a
new API key and breaks every deployed client holding the old one. For a
product whose premise is governed access to data sources, "you cannot grant an
existing agent access to a new source" is a gap rather than an inconvenience.

Nothing here teaches the system anything new about enforcement. Migration 007
already gave `identity_source_role_grants` an addressable primary key,
`enabled`, `expires_at` and `granted_by`, and both read paths - the source-role
evaluator and the authenticator - already filter on `enabled` and `expires_at`.
Because the authenticator revalidates against Postgres on every request and
caches no grants, a revocation here takes effect on the agent's very next call.
This module is the write half that was missing.

Two deliberate choices:

* A revoke disables the row; it never deletes it. "Did this agent ever hold
  access to payroll, and when was it withdrawn?" is the question an incident
  review asks, and a deleted row cannot answer it.
* A grant names one role. Replacing an identity's whole set in one call reads
  more conveniently but loses updates: two administrators with the page open,
  one adding and one removing, and the later write silently discards the
  other's change. On authorization state that is a security bug, not a
  usability one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class GrantError(ValueError):
    """A grant could not be made as asked."""


class UnknownRoleError(GrantError):
    """No enabled source role matches the requested key or id."""


class RoleSourceMismatchError(GrantError):
    """The role belongs to a different source than the grant names.

    Worth its own type because the failure is otherwise invisible: the read
    path joins `source_roles` on both `role_id` *and* `source_id`, so a grant
    carrying a role from another source is stored happily and then never
    matches anything. It looks like a successful grant and behaves like no
    grant at all.
    """


@dataclass(frozen=True, slots=True)
class SourceRoleGrant:
    """One identity's hold on one role of one source."""

    id: int
    identity_id: int
    source_id: str
    role_id: int
    role_key: str
    enabled: bool
    expires_at: datetime | None
    granted_by: int | None
    created_at: datetime
    updated_at: datetime


def _row_to_grant(row: Any) -> SourceRoleGrant:
    return SourceRoleGrant(
        id=int(row["id"]),
        identity_id=int(row["identity_id"]),
        source_id=str(row["source_id"]),
        role_id=int(row["role_id"]),
        role_key=str(row["role_key"]),
        enabled=bool(row["enabled"]),
        expires_at=row["expires_at"],
        granted_by=None if row["granted_by"] is None else int(row["granted_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_RESOLVE_BY_KEY = """
SELECT id, role_key, source_id
FROM source_roles
WHERE source_id = $1 AND role_key = lower($2) AND enabled = TRUE
"""

_RESOLVE_BY_ID = """
SELECT id, role_key, source_id
FROM source_roles
WHERE id = $1 AND enabled = TRUE
"""


async def resolve_role(
    pool: Any,
    *,
    source_id: str,
    role_key: str | None = None,
    role_id: int | None = None,
) -> tuple[int, str]:
    """Resolve a role reference to `(role_id, role_key)` for this source.

    Refuses a role belonging to a different source. That check is the reason
    this is a function rather than an inline lookup: the previous write path
    resolved a `role_key` scoped to the source but used a supplied `role_id`
    unchecked, so a cross-source id produced a permanently inert grant.
    """
    if role_id is not None:
        row = await pool.fetchrow(_RESOLVE_BY_ID, int(role_id))
        if row is None:
            raise UnknownRoleError(f"No enabled source role with id {role_id}")
        if str(row["source_id"]) != source_id:
            raise RoleSourceMismatchError(
                f"Source role {role_id} belongs to source {row['source_id']!r}, "
                f"not {source_id!r}; such a grant could never match a request"
            )
        return int(row["id"]), str(row["role_key"])

    if not role_key:
        raise GrantError("A grant needs either a role_key or a role_id")

    row = await pool.fetchrow(_RESOLVE_BY_KEY, source_id, role_key)
    if row is None:
        raise UnknownRoleError(f"No enabled source role {role_key!r} on source {source_id!r}")
    return int(row["id"]), str(row["role_key"])


_GRANT_SQL = """
INSERT INTO identity_source_role_grants
    (identity_id, source_id, role_id, enabled, expires_at, granted_by, metadata)
VALUES ($1, $2, $3, TRUE, $4, $5, $6::jsonb)
ON CONFLICT (identity_id, source_id, role_id) DO UPDATE
SET enabled    = TRUE,
    expires_at = EXCLUDED.expires_at,
    granted_by = COALESCE(EXCLUDED.granted_by, identity_source_role_grants.granted_by),
    metadata   = EXCLUDED.metadata,
    updated_at = NOW()
RETURNING id, identity_id, source_id, role_id, enabled, expires_at, granted_by,
          created_at, updated_at
"""


async def grant(
    pool: Any,
    *,
    identity_id: int,
    source_id: str,
    role_key: str | None = None,
    role_id: int | None = None,
    granted_by: int | None = None,
    expires_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> SourceRoleGrant:
    """Give an identity a role on a source, or restate an existing hold.

    Idempotent: granting a role the identity already holds re-enables it and
    replaces its terms, because a second grant is a statement of current
    intent rather than a duplicate. An omitted `expires_at` therefore means
    "no expiry", including when the previous grant had one.

    `granted_by` is only ever filled in, never cleared, so a grant made
    through a path that does not know the acting administrator cannot erase
    the attribution of one that did.
    """
    resolved_id, resolved_key = await resolve_role(
        pool, source_id=source_id, role_key=role_key, role_id=role_id
    )
    row = await pool.fetchrow(
        _GRANT_SQL,
        identity_id,
        source_id,
        resolved_id,
        expires_at,
        granted_by,
        metadata or {"created_from": "source_role_grants"},
    )
    return _row_to_grant({**dict(row), "role_key": resolved_key})


_REVOKE_SQL = """
UPDATE identity_source_role_grants g
SET enabled = FALSE, updated_at = NOW()
FROM source_roles r
WHERE g.id = $1 AND g.identity_id = $2 AND r.id = g.role_id
RETURNING g.id, g.identity_id, g.source_id, g.role_id, r.role_key, g.enabled,
          g.expires_at, g.granted_by, g.created_at, g.updated_at
"""


async def revoke(pool: Any, *, identity_id: int, grant_id: int) -> SourceRoleGrant | None:
    """Withdraw a grant, keeping the row.

    Returns the grant, or None when the identity has no such grant. Revoking
    an already-revoked grant succeeds and returns it: the caller asked for a
    state, and the state holds.

    The agent is not re-keyed and its other grants are untouched. The change
    takes effect on its next request, because the authenticator revalidates
    against Postgres per request rather than caching grants.
    """
    row = await pool.fetchrow(_REVOKE_SQL, int(grant_id), int(identity_id))
    return None if row is None else _row_to_grant(row)


_LIST_SQL = """
SELECT g.id, g.identity_id, g.source_id, g.role_id, r.role_key, g.enabled,
       g.expires_at, g.granted_by, g.created_at, g.updated_at
FROM identity_source_role_grants g
JOIN source_roles r ON r.id = g.role_id
WHERE g.identity_id = $1
  AND ($2::boolean OR g.enabled = TRUE)
ORDER BY g.source_id, r.role_key
"""


async def list_grants(
    pool: Any, *, identity_id: int, include_revoked: bool = False
) -> list[SourceRoleGrant]:
    """Every grant this identity holds, optionally including withdrawn ones.

    Revoked grants are excluded by default so a caller asking "what can this
    agent reach" is not answered with history.
    """
    rows = await pool.fetch(_LIST_SQL, int(identity_id), include_revoked)
    return [_row_to_grant(row) for row in rows]
