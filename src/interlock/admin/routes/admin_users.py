"""JSON API for console users: the people who sign in to the admin console.

Administrators only (owner or admin); the auth middleware enforces that. The
rules live in interlock.admin.admin_users and are shared with the console pages.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from interlock.admin import admin_users as users

router = APIRouter(prefix="/api/admin-users", tags=["admin-users"])


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    user_type: str = Field(description="administrator, read_write, read_only or custom")
    roles: list[str] = Field(default_factory=list, description="Roles for a custom user")
    email: str | None = None
    sso_subject: str | None = None


class AdminUserUpdate(BaseModel):
    user_type: str = Field(description="administrator, read_write, read_only or custom")
    roles: list[str] = Field(default_factory=list, description="Roles for a custom user")
    email: str | None = None
    sso_subject: str | None = None


def _error(exc: users.AdminUserError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=exc.status)


@router.get("")
async def list_console_users(request: Request) -> list[dict[str, Any]]:
    """List console users."""
    return [user.to_public() for user in await users.list_admin_users(request.app.state.pg_pool)]


@router.post("", status_code=201, response_model=None)
async def create_console_user(body: AdminUserCreate, request: Request) -> Any:
    """Create a console user; returns its temporary password once."""
    try:
        user, password = await users.create_admin_user(
            request.app.state.pg_pool,
            username=body.username,
            user_type=body.user_type,
            custom_roles=body.roles,
            email=body.email,
            sso_subject=body.sso_subject,
            sso_enabled=users.sso_enabled(request),
        )
    except users.AdminUserError as exc:
        return _error(exc)
    await users.audit_change(request, action="create", user=user, status_code=201)
    return JSONResponse(
        {"user": jsonable(user.to_public()), "temporary_password": password},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{user_id}", response_model=None)
async def get_console_user(user_id: int, request: Request) -> Any:
    """Show one console user."""
    try:
        user = await users.get_admin_user(request.app.state.pg_pool, user_id)
    except users.AdminUserError as exc:
        return _error(exc)
    return user.to_public()


@router.put("/{user_id}", response_model=None)
async def update_console_user(user_id: int, body: AdminUserUpdate, request: Request) -> Any:
    """Change a console user's type or roles, email and SSO subject."""
    try:
        before, after = await users.update_admin_user(
            request.app.state.pg_pool,
            actor_id=users.actor_id(request),
            user_id=user_id,
            user_type=body.user_type,
            custom_roles=body.roles,
            email=body.email,
            sso_subject=body.sso_subject,
            sso_enabled=users.sso_enabled(request),
        )
    except users.AdminUserError as exc:
        return _error(exc)
    await users.audit_change(
        request,
        action="update",
        user=after,
        before=before,
        changed_fields=users.changed(before, after),
    )
    return after.to_public()


@router.post("/{user_id}/reset-password", response_model=None)
async def reset_console_user_password(user_id: int, request: Request) -> Any:
    """Give a console user a new temporary password, shown once."""
    try:
        user, password = await users.reset_password(
            request.app.state.pg_pool, actor_id=users.actor_id(request), user_id=user_id
        )
    except users.AdminUserError as exc:
        return _error(exc)
    await users.audit_change(request, action="reset_password", user=user)
    return JSONResponse(
        {"user": jsonable(user.to_public()), "temporary_password": password},
        headers={"Cache-Control": "no-store"},
    )


async def _set_enabled(request: Request, user_id: int, enabled: bool) -> Any:
    try:
        before, after = await users.set_enabled(
            request.app.state.pg_pool,
            actor_id=users.actor_id(request),
            user_id=user_id,
            enabled=enabled,
        )
    except users.AdminUserError as exc:
        return _error(exc)
    await users.audit_change(
        request,
        action="enable" if enabled else "disable",
        user=after,
        before=before,
        changed_fields=users.changed(before, after),
    )
    return after.to_public()


@router.post("/{user_id}/disable", response_model=None)
async def disable_console_user(user_id: int, request: Request) -> Any:
    """Disable a console user and end their sessions."""
    return await _set_enabled(request, user_id, False)


@router.post("/{user_id}/enable", response_model=None)
async def enable_console_user(user_id: int, request: Request) -> Any:
    """Enable a disabled console user."""
    return await _set_enabled(request, user_id, True)


@router.delete("/{user_id}", status_code=204, response_model=None)
async def delete_console_user(user_id: int, request: Request) -> Any:
    """Delete a disabled console user."""
    try:
        before = await users.delete_admin_user(
            request.app.state.pg_pool, actor_id=users.actor_id(request), user_id=user_id
        )
    except users.AdminUserError as exc:
        return _error(exc)
    await users.audit_change(request, action="delete", user=before, before=before, status_code=204)
    return Response(status_code=204)


def jsonable(data: dict[str, Any]) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    result: dict[str, Any] = jsonable_encoder(data)
    return result
