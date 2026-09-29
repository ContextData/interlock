"""CRUD routes for the policy_rules table."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from interlock.admin.audit import (
    audit_admin_action,
    mutation_audit_detail,
    summarize_admin_resource,
)

router = APIRouter(prefix="/api/policies", tags=["policies"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PolicyCreate(BaseModel):
    name: str
    priority: int = 0
    conditions: dict[str, Any]
    actions: dict[str, Any]
    enabled: bool = True


class PolicyUpdate(BaseModel):
    name: str | None = None
    priority: int | None = None
    conditions: dict[str, Any] | None = None
    actions: dict[str, Any] | None = None
    enabled: bool | None = None


class PolicyResponse(BaseModel):
    id: int
    name: str
    priority: int
    conditions: dict[str, Any]
    actions: dict[str, Any]
    enabled: bool
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    # With the custom JSON codec on the pool, JSONB columns come back as dicts.
    # Guard against str for environments without the codec (e.g. test mocks).
    for key in ("conditions", "actions"):
        val = d.get(key)
        if isinstance(val, str):
            d[key] = json.loads(val)
        elif val is None:
            d[key] = {}
    return d


async def _notify(request: Request) -> None:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        await conn.execute("NOTIFY onyx_policy_changed")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PolicyResponse])
async def list_policies(request: Request) -> list[dict[str, Any]]:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM policy_rules ORDER BY priority DESC, id")
    return [_row_to_dict(r) for r in rows]


@router.get("/{policy_id}", response_model=PolicyResponse)
async def get_policy(policy_id: int, request: Request) -> dict[str, Any]:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM policy_rules WHERE id = $1", policy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Policy rule not found")
    return _row_to_dict(row)


@router.post("", response_model=PolicyResponse, status_code=201)
async def create_policy(body: PolicyCreate, request: Request) -> dict[str, Any]:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            body.name,
            body.priority,
            body.conditions,
            body.actions,
            body.enabled,
        )
    await _notify(request)
    response = _row_to_dict(row)
    await audit_admin_action(
        request,
        action="policy.create",
        resource="policy_rule",
        resource_id=str(response["id"]),
        success=True,
        detail=mutation_audit_detail(
            after=summarize_admin_resource(
                response,
                fields=("id", "name", "priority", "conditions", "actions", "enabled"),
            ),
            changed_fields=("name", "priority", "conditions", "actions", "enabled"),
            status_code=201,
        ),
    )
    return response


@router.put("/{policy_id}", response_model=PolicyResponse)
async def update_policy(policy_id: int, body: PolicyUpdate, request: Request) -> dict[str, Any]:
    pool = request.app.state.pg_pool

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses: list[str] = []
    values: list[Any] = []
    idx = 1

    for field, value in updates.items():
        if field in ("conditions", "actions"):
            set_clauses.append(f"{field} = ${idx}")
            values.append(value)
        else:
            set_clauses.append(f"{field} = ${idx}")
            values.append(value)
        idx += 1

    values.append(policy_id)

    query = f"""
        UPDATE policy_rules
        SET {', '.join(set_clauses)}
        WHERE id = ${idx}
        RETURNING *
    """

    async with pool.acquire() as conn:
        before_row = await conn.fetchrow("SELECT * FROM policy_rules WHERE id = $1", policy_id)
        row = await conn.fetchrow(query, *values)
    if row is None:
        raise HTTPException(status_code=404, detail="Policy rule not found")
    await _notify(request)
    response = _row_to_dict(row)
    await audit_admin_action(
        request,
        action="policy.update",
        resource="policy_rule",
        resource_id=str(policy_id),
        success=True,
        detail=mutation_audit_detail(
            before=summarize_admin_resource(
                before_row,
                fields=("id", "name", "priority", "conditions", "actions", "enabled"),
            ),
            after=summarize_admin_resource(
                response,
                fields=("id", "name", "priority", "conditions", "actions", "enabled"),
            ),
            changed_fields=tuple(updates.keys()),
            status_code=200,
        ),
    )
    return response


@router.delete("/{policy_id}", status_code=204)
async def delete_policy(policy_id: int, request: Request) -> None:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        before_row = await conn.fetchrow("SELECT * FROM policy_rules WHERE id = $1", policy_id)
        result = await conn.execute("DELETE FROM policy_rules WHERE id = $1", policy_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Policy rule not found")
    await _notify(request)
    await audit_admin_action(
        request,
        action="policy.delete",
        resource="policy_rule",
        resource_id=str(policy_id),
        success=True,
        detail=mutation_audit_detail(
            before=summarize_admin_resource(
                before_row,
                fields=("id", "name", "priority", "conditions", "actions", "enabled"),
            ),
            status_code=204,
        ),
    )
