"""CRUD routes for the write_approval_queue table."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from interlock.admin.audit import (
    audit_admin_action,
    mutation_audit_detail,
    summarize_admin_resource,
)
from interlock.errors import InterLockError
from interlock.security.approval_redaction import (
    _SECRET_MARKERS as SECRET_MARKERS,
)
from interlock.security.approval_redaction import (
    redact_body_metadata,
    redact_metadata,
    redact_sql_literals,
)

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ApproveRequest(BaseModel):
    approved_by: str | None = None


class RejectRequest(BaseModel):
    rejected_by: str | None = None


class ApprovalResponse(BaseModel):
    id: int
    identity_id: int
    source_id: str
    sql_text: str
    risk_level: str
    status: str
    approved_by: str | None = None
    executed_at: datetime | None = None
    expires_at: datetime | None = None
    request_metadata: dict[str, Any] = {}
    created_at: datetime | None = None


class ApprovalActionResponse(BaseModel):
    id: int
    status: str
    executed: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row) -> dict[str, Any]:
    """Convert an asyncpg Record to a plain dict."""
    d = dict(row)
    # With the custom JSON codec on the pool, JSONB columns come back as dicts.
    # Guard against str for environments without the codec (e.g. test mocks).
    meta = d.get("request_metadata")
    if isinstance(meta, str):
        d["request_metadata"] = json.loads(meta)
    elif meta is None:
        d["request_metadata"] = {}
    return _redact_approval_for_api(d)


def _redact_approval_for_api(row: dict[str, Any]) -> dict[str, Any]:
    safe = dict(row)
    sql_text = str(safe.get("sql_text") or "")
    if sql_text:
        safe["sql_text"] = _redact_sql_literals(sql_text)
    safe["request_metadata"] = _redact_metadata(safe.get("request_metadata") or {})
    return safe


# Hoisted to interlock.security.approval_redaction so outbound notifications
# apply the same masking. These aliases keep this module's call sites and its
# tests unchanged.
_redact_sql_literals = redact_sql_literals
_SECRET_MARKERS = SECRET_MARKERS
_redact_metadata = redact_metadata
_redact_body_metadata = redact_body_metadata


def _approval_summary(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return summarize_admin_resource(
        _row_to_dict(row),
        fields=(
            "id",
            "identity_id",
            "source_id",
            "sql_text",
            "risk_level",
            "status",
            "approved_by",
            "executed_at",
            "expires_at",
            "request_metadata",
            "created_at",
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


_LIST_SQL = """
SELECT id, identity_id, source_id, sql_text, risk_level,
       status, approved_by, executed_at, expires_at,
       request_metadata, created_at
FROM write_approval_queue
ORDER BY created_at DESC
LIMIT 100
"""

_LIST_FILTERED_SQL = """
SELECT id, identity_id, source_id, sql_text, risk_level,
       status, approved_by, executed_at, expires_at,
       request_metadata, created_at
FROM write_approval_queue
WHERE status = $1
ORDER BY created_at DESC
LIMIT 100
"""

_GET_SQL = """
SELECT id, identity_id, source_id, sql_text, risk_level,
       status, approved_by, executed_at, expires_at,
       request_metadata, created_at
FROM write_approval_queue
WHERE id = $1
"""

_APPROVE_SQL = """
UPDATE write_approval_queue
SET status = 'approved', approved_by = $2
WHERE id = $1
  AND status = 'pending'
  AND (expires_at IS NULL OR expires_at > NOW())
"""

_REJECT_SQL = """
UPDATE write_approval_queue
SET status = 'rejected', approved_by = $2
WHERE id = $1 AND status = 'pending'
"""


@router.get("", response_model=list[ApprovalResponse])
async def list_approvals(
    request: Request,
    status: str | None = Query(
        None, pattern="^(pending|approved|rejected|expired|executed|failed)$"
    ),
) -> list[dict[str, Any]]:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        if status is not None:
            rows = await conn.fetch(_LIST_FILTERED_SQL, status)
        else:
            rows = await conn.fetch(_LIST_SQL)
    return [_row_to_dict(r) for r in rows]


@router.get("/{approval_id}", response_model=ApprovalResponse)
async def get_approval(approval_id: int, request: Request) -> dict[str, Any]:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_GET_SQL, approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _row_to_dict(row)


@router.post("/{approval_id}/approve", response_model=ApprovalActionResponse)
async def approve_approval(approval_id: int, request: Request) -> dict[str, Any] | HTMLResponse:
    actor = _admin_actor(request, await _optional_action_field(request, "approved_by"))
    comment = await _optional_action_field(request, "comment")
    before_row = await _fetch_approval_row(request, approval_id)
    queue = getattr(request.app.state, "approval_queue", None)
    if queue is None:
        response = await _approve_without_service(approval_id, actor, request)
    else:
        try:
            result = await queue.approve(approval_id, actor)
        except InterLockError as exc:
            pool = request.app.state.pg_pool
            async with pool.acquire() as conn:
                row = await conn.fetchrow(_GET_SQL, approval_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Approval not found") from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response = {
            "id": approval_id,
            "status": "executed" if result.get("executed") else result["status"],
            "executed": bool(result.get("executed")),
        }
    await audit_admin_action(
        request,
        action="approval.approve",
        resource="write_approval",
        resource_id=str(approval_id),
        success=True,
        detail=mutation_audit_detail(
            before=_approval_summary(before_row),
            after={"id": approval_id, **response},
            changed_fields=("status", "approved_by", "executed_at"),
            status_code=200,
            extra={"actor": actor, "comment": comment} if comment else {"actor": actor},
        ),
    )
    if request.headers.get("HX-Request") == "true":
        return HTMLResponse("", headers={"HX-Redirect": "/dashboard/write-safety"})
    return response


@router.post("/{approval_id}/reject", response_model=ApprovalActionResponse)
async def reject_approval(approval_id: int, request: Request) -> dict[str, Any] | HTMLResponse:
    actor = _admin_actor(request, await _optional_action_field(request, "rejected_by"))
    comment = await _optional_action_field(request, "comment")
    before_row = await _fetch_approval_row(request, approval_id)
    queue = getattr(request.app.state, "approval_queue", None)
    if queue is None:
        response = await _reject_without_service(approval_id, actor, request)
    else:
        try:
            await queue.reject(approval_id, actor)
        except InterLockError as exc:
            pool = request.app.state.pg_pool
            async with pool.acquire() as conn:
                row = await conn.fetchrow(_GET_SQL, approval_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Approval not found") from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response = {"id": approval_id, "status": "rejected"}

    await audit_admin_action(
        request,
        action="approval.reject",
        resource="write_approval",
        resource_id=str(approval_id),
        success=True,
        detail=mutation_audit_detail(
            before=_approval_summary(before_row),
            after=response,
            changed_fields=("status", "approved_by"),
            status_code=200,
            extra={"actor": actor, "comment": comment} if comment else {"actor": actor},
        ),
    )
    if request.headers.get("HX-Request") == "true":
        return HTMLResponse("", headers={"HX-Redirect": "/dashboard/write-safety"})

    return response


def _admin_actor(request: Request, fallback: str | None = None) -> str:
    """Return authenticated admin identity for approval audit fields."""
    admin = getattr(request.state, "admin", None)
    if admin is not None and getattr(admin, "username", None):
        return admin.username
    return fallback or "admin-api"


async def _fetch_approval_row(request: Request, approval_id: int) -> Any:
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        return await conn.fetchrow(_GET_SQL, approval_id)


async def _optional_action_field(request: Request, field: str) -> str | None:
    """Read optional actor/comment fields from JSON or form HTMX posts."""
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, dict):
                raw = data.get(field)
                return str(raw) if raw not in (None, "") else None
        if (
            "application/x-www-form-urlencoded" in content_type
            or "multipart/form-data" in content_type
        ):
            form = await request.form()
            raw = form.get(field)
            return str(raw) if raw not in (None, "") else None
    except Exception:
        return None
    return None


async def _approve_without_service(
    approval_id: int, approved_by: str, request: Request
) -> dict[str, Any]:
    """Compatibility fallback for tests and minimally wired admin apps."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_GET_SQL, approval_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        if row["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Approval is '{row['status']}', not pending",
            )
        expires_at = row.get("expires_at") if hasattr(row, "get") else row["expires_at"]
        if expires_at is not None and expires_at <= datetime.now(expires_at.tzinfo):
            raise HTTPException(
                status_code=409,
                detail="Approval is expired",
            )
        await conn.execute(_APPROVE_SQL, approval_id, approved_by)
        await conn.execute(
            "SELECT pg_notify('onyx_write_approval', $1)",
            json.dumps({"approval_id": approval_id, "status": "approved"}),
        )
    return {"id": approval_id, "status": "approved", "executed": False}


async def _reject_without_service(
    approval_id: int, rejected_by: str, request: Request
) -> dict[str, Any]:
    """Compatibility fallback for tests and minimally wired admin apps."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_GET_SQL, approval_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        if row["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Approval is '{row['status']}', not pending",
            )
        await conn.execute(_REJECT_SQL, approval_id, rejected_by)
    return {"id": approval_id, "status": "rejected", "executed": False}
