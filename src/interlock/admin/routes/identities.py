"""CRUD routes for the identities table."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from interlock.admin.audit import (
    audit_admin_action,
    mutation_audit_detail,
    summarize_admin_resource,
)
from interlock.core import source_role_grants
from interlock.models import AgentType
from interlock.security.api_keys import hash_api_key_for_storage, validate_custom_api_key

router = APIRouter(prefix="/api/identities", tags=["identities"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def _validated_agent_type(value: str | None) -> str | None:
    """Reject an agent_type the runtime cannot read back.

    `identities.agent_type` is parsed into the AgentType enum when an identity
    authenticates. A value outside the enum therefore creates an identity that
    can never be used, and the failure surfaces as 401 "Invalid API key" -
    pointing an operator at the credential when the row is what is wrong.
    Rejecting it here means the API cannot create that state.
    """
    if value is None:
        return None
    allowed = {member.value for member in AgentType}
    if value not in allowed:
        raise ValueError(
            f"agent_type must be one of {sorted(allowed)}; got {value!r}. "
            "An unrecognised value creates an identity that cannot authenticate."
        )
    return value


class IdentityCreate(BaseModel):
    name: str
    api_key: str  # raw key - never persisted
    agent_type: str = "custom"

    _check_agent_type = field_validator("agent_type")(_validated_agent_type)
    team: str | None = None
    roles: list[str] = Field(default_factory=list)
    mapped_pg_role: str | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class IdentityUpdate(BaseModel):
    name: str | None = None
    agent_type: str | None = None

    _check_agent_type = field_validator("agent_type")(_validated_agent_type)
    team: str | None = None
    roles: list[str] | None = None
    mapped_pg_role: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class IdentityResponse(BaseModel):
    """Response model - deliberately excludes api_key_hash."""

    id: int
    name: str
    agent_type: str
    team: str | None = None
    roles: list[str]
    mapped_pg_role: str | None = None
    enabled: bool
    metadata: dict[str, Any]
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    # Remove the hash from the response
    d.pop("api_key_hash", None)
    d.pop("api_key_hash_version", None)
    # With the custom JSON codec on the pool, JSONB columns come back as dicts.
    # Guard against str for environments without the codec (e.g. test mocks).
    val = d.get("metadata")
    if isinstance(val, str):
        d["metadata"] = json.loads(val)
    elif val is None:
        d["metadata"] = {}
    # asyncpg returns TEXT[] as list already, but ensure it
    if d.get("roles") is None:
        d["roles"] = []
    return d


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[IdentityResponse])
async def list_identities(request: Request) -> list[dict[str, Any]]:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM identities ORDER BY id")
    return [_row_to_dict(r) for r in rows]


@router.get("/{identity_id}", response_model=IdentityResponse)
async def get_identity(identity_id: int, request: Request) -> dict[str, Any]:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM identities WHERE id = $1", identity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Identity not found")
    return _row_to_dict(row)


@router.post("", response_model=IdentityResponse, status_code=201)
async def create_identity(body: IdentityCreate, request: Request) -> dict[str, Any]:
    pool = request.app.state.pg_pool
    auth_config = request.app.state.config.auth
    if not validate_custom_api_key(body.api_key, auth_config):
        raise HTTPException(status_code=422, detail="API key does not meet entropy requirements")
    key_hash, key_hash_version = hash_api_key_for_storage(body.api_key, auth_config)

    async with pool.acquire() as conn:
        # Check for duplicate api_key_hash
        existing = await conn.fetchval("SELECT 1 FROM identities WHERE api_key_hash = $1", key_hash)
        if existing:
            raise HTTPException(
                status_code=409, detail="An identity with this API key already exists"
            )
        row = await conn.fetchrow(
            """
            INSERT INTO identities (name, api_key_hash, api_key_hash_version,
                                    agent_type, team, roles,
                                    mapped_pg_role, enabled, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            body.name,
            key_hash,
            key_hash_version,
            body.agent_type,
            body.team,
            body.roles,
            body.mapped_pg_role,
            body.enabled,
            body.metadata,
        )
    response = _row_to_dict(row)
    await audit_admin_action(
        request,
        action="identity.create",
        resource="identity",
        resource_id=str(response["id"]),
        success=True,
        detail=mutation_audit_detail(
            after=summarize_admin_resource(
                response,
                fields=(
                    "id",
                    "name",
                    "agent_type",
                    "team",
                    "roles",
                    "mapped_pg_role",
                    "enabled",
                    "metadata",
                ),
            ),
            changed_fields=(
                "name",
                "agent_type",
                "team",
                "roles",
                "mapped_pg_role",
                "enabled",
                "metadata",
            ),
            status_code=201,
        ),
    )
    return response


@router.put("/{identity_id}", response_model=IdentityResponse)
async def update_identity(
    identity_id: int, body: IdentityUpdate, request: Request
) -> dict[str, Any]:
    pool = request.app.state.pg_pool

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses: list[str] = []
    values: list[Any] = []
    idx = 1

    for field, value in updates.items():
        if field == "metadata":
            set_clauses.append(f"{field} = ${idx}")
            values.append(value)
        else:
            set_clauses.append(f"{field} = ${idx}")
            values.append(value)
        idx += 1

    set_clauses.append("updated_at = NOW()")
    values.append(identity_id)

    query = f"""
        UPDATE identities
        SET {', '.join(set_clauses)}
        WHERE id = ${idx}
        RETURNING *
    """

    async with pool.acquire() as conn:
        before_row = await conn.fetchrow("SELECT * FROM identities WHERE id = $1", identity_id)
        row = await conn.fetchrow(query, *values)
    if row is None:
        raise HTTPException(status_code=404, detail="Identity not found")
    response = _row_to_dict(row)
    await audit_admin_action(
        request,
        action="identity.update",
        resource="identity",
        resource_id=str(identity_id),
        success=True,
        detail=mutation_audit_detail(
            before=summarize_admin_resource(
                before_row,
                fields=(
                    "id",
                    "name",
                    "agent_type",
                    "team",
                    "roles",
                    "mapped_pg_role",
                    "enabled",
                    "metadata",
                ),
            ),
            after=summarize_admin_resource(
                response,
                fields=(
                    "id",
                    "name",
                    "agent_type",
                    "team",
                    "roles",
                    "mapped_pg_role",
                    "enabled",
                    "metadata",
                ),
            ),
            changed_fields=tuple(updates.keys()),
            status_code=200,
        ),
    )
    return response


@router.delete("/{identity_id}", status_code=204)
async def delete_identity(identity_id: int, request: Request) -> None:
    pool = request.app.state.pg_pool
    admin_id = getattr(getattr(request.state, "admin", None), "admin_id", None)
    async with pool.acquire() as conn:
        tx = conn.transaction() if hasattr(conn, "transaction") else nullcontext()
        async with tx:
            before_row = await conn.fetchrow("SELECT * FROM identities WHERE id = $1", identity_id)
            if before_row is None:
                raise HTTPException(status_code=404, detail="Identity not found")
            # audit_log keeps only the id, so the name is kept here for the
            # audit views to show once the identity row is gone.
            await conn.execute(
                """
                INSERT INTO identity_tombstones (identity_id, name, team, deleted_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (identity_id) DO UPDATE
                SET name = EXCLUDED.name, team = EXCLUDED.team,
                    deleted_at = NOW(), deleted_by = EXCLUDED.deleted_by
                """,
                identity_id,
                before_row["name"],
                before_row["team"],
                admin_id,
            )
            result = await conn.execute("DELETE FROM identities WHERE id = $1", identity_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Identity not found")
    await audit_admin_action(
        request,
        action="identity.delete",
        resource="identity",
        resource_id=str(identity_id),
        success=True,
        detail=mutation_audit_detail(
            before=summarize_admin_resource(
                before_row,
                fields=(
                    "id",
                    "name",
                    "agent_type",
                    "team",
                    "roles",
                    "mapped_pg_role",
                    "enabled",
                    "metadata",
                ),
            ),
            status_code=204,
        ),
    )


# ---------------------------------------------------------------------------
# Source-role grants
#
# An identity's grants were writable only by the create handler, so the only
# way to give an existing agent access to a newly registered source was to
# delete it and recreate it - which mints a new API key and breaks every
# deployed client holding the old one. These three routes make a grant a thing
# in its own right: addressable, revocable and attributable, which is what the
# table has always been shaped for.
# ---------------------------------------------------------------------------


class SourceRoleGrantCreate(BaseModel):
    source_id: str = Field(min_length=1)
    role_key: str | None = None
    role_id: int | None = None
    expires_at: datetime | None = None


class SourceRoleGrantResponse(BaseModel):
    id: int
    identity_id: int
    source_id: str
    role_id: int
    role_key: str
    enabled: bool
    expires_at: datetime | None
    granted_by: int | None


def _grant_to_dict(row: source_role_grants.SourceRoleGrant) -> dict[str, Any]:
    return {
        "id": row.id,
        "identity_id": row.identity_id,
        "source_id": row.source_id,
        "role_id": row.role_id,
        "role_key": row.role_key,
        "enabled": row.enabled,
        "expires_at": row.expires_at,
        "granted_by": row.granted_by,
    }


async def _require_identity(conn: Any, identity_id: int) -> None:
    if not await conn.fetchval("SELECT 1 FROM identities WHERE id = $1", identity_id):
        raise HTTPException(status_code=404, detail="Identity not found")


@router.get(
    "/{identity_id}/source-role-grants",
    response_model=list[SourceRoleGrantResponse],
)
async def list_identity_grants(
    identity_id: int, request: Request, include_revoked: bool = False
) -> list[dict[str, Any]]:
    """What this agent can reach. Revoked grants are history, not access, so
    they are excluded unless asked for."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        await _require_identity(conn, identity_id)
        grants = await source_role_grants.list_grants(
            conn, identity_id=identity_id, include_revoked=include_revoked
        )
    return [_grant_to_dict(row) for row in grants]


@router.post(
    "/{identity_id}/source-role-grants",
    response_model=SourceRoleGrantResponse,
    status_code=201,
)
async def create_identity_grant(
    identity_id: int, body: SourceRoleGrantCreate, request: Request
) -> dict[str, Any]:
    """Grant a role on a source, without touching the identity's API key.

    Idempotent: granting a role the identity already holds restates its terms
    and re-enables it if it had been revoked.
    """
    pool = request.app.state.pg_pool
    admin_id = getattr(getattr(request.state, "admin", None), "admin_id", None)
    try:
        async with pool.acquire() as conn:
            await _require_identity(conn, identity_id)
            created = await source_role_grants.grant(
                conn,
                identity_id=identity_id,
                source_id=body.source_id,
                role_key=body.role_key,
                role_id=body.role_id,
                granted_by=admin_id,
                expires_at=body.expires_at,
                metadata={"created_from": "api"},
            )
            # Emitted for parity with the other identity mutations.
            # Enforcement does not depend on it: the authenticator
            # revalidates against Postgres per request and caches no grants.
            await conn.execute(
                "SELECT pg_notify('onyx_identity_changed', $1)",
                json.dumps({"identity_id": identity_id, "source_roles_changed": True}),
            )
    except source_role_grants.GrantError as exc:
        await audit_admin_action(
            request,
            action="identity.grant.create",
            resource="identity_source_role_grant",
            resource_id=str(identity_id),
            success=False,
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await audit_admin_action(
        request,
        action="identity.grant.create",
        resource="identity_source_role_grant",
        resource_id=str(created.id),
        success=True,
        detail=mutation_audit_detail(after=_grant_to_dict(created)),
    )
    return _grant_to_dict(created)


@router.delete("/{identity_id}/source-role-grants/{grant_id}", status_code=200)
async def revoke_identity_grant(
    identity_id: int, grant_id: int, request: Request
) -> dict[str, Any]:
    """Withdraw a grant. The row is kept, disabled.

    Deleting it would make "did this agent ever hold access, and when was it
    withdrawn" unanswerable, which is the question an incident review asks.
    The agent is not re-keyed and its other grants are untouched; the change
    takes effect on its next request.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        await _require_identity(conn, identity_id)
        revoked = await source_role_grants.revoke(conn, identity_id=identity_id, grant_id=grant_id)
        if revoked is None:
            raise HTTPException(status_code=404, detail="Grant not found for this identity")
        await conn.execute(
            "SELECT pg_notify('onyx_identity_changed', $1)",
            json.dumps({"identity_id": identity_id, "source_roles_changed": True}),
        )

    await audit_admin_action(
        request,
        action="identity.grant.revoke",
        resource="identity_source_role_grant",
        resource_id=str(grant_id),
        success=True,
        detail=mutation_audit_detail(after=_grant_to_dict(revoked)),
    )
    return _grant_to_dict(revoked)
