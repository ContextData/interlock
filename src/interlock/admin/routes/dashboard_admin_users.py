"""Console pages for console users. Administrators only (see auth_middleware)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from interlock.admin import admin_users as users
from interlock.admin.roles import CUSTOM_ROLES, CUSTOM_TYPE, ROLE_MEANINGS
from interlock.admin.routes.dashboard import _mutation_redirect, _render, _render_page

router = APIRouter(tags=["dashboard"])

_USER_TYPES = (
    ("administrator", "Administrator", "Everything, including adding and removing console users."),
    (
        "read_write",
        "Read-write",
        "Change sources, policies, agents and their keys, and review approvals. "
        "Cannot manage console users.",
    ),
    ("read_only", "Read-only", "See every page; change nothing."),
    (CUSTOM_TYPE, "Custom", "Pick specific roles, for example only reviewing approvals."),
)
_EMPTY_FORM: dict[str, Any] = {
    "id": None,
    "username": "",
    "user_type": "read_only",
    "roles": [],
    "email": "",
    "sso_subject": "",
}


def _form_context(
    request: Request, mode: str, form: dict[str, Any], error: str = ""
) -> dict[str, Any]:
    return {
        "active_page": "admin-users",
        "mode": mode,
        "form": form,
        "error": error,
        "user_types": _USER_TYPES,
        "custom_roles": [(role, ROLE_MEANINGS[role]) for role in CUSTOM_ROLES],
        "sso_enabled": users.sso_enabled(request),
    }


async def _posted(request: Request) -> dict[str, Any]:
    data = await request.form()
    return {
        "username": str(data.get("username") or ""),
        "user_type": str(data.get("user_type") or ""),
        "roles": [str(role) for role in data.getlist("roles")],
        "email": str(data.get("email") or ""),
        "sso_subject": str(data.get("sso_subject") or ""),
    }


def _error(request: Request, exc: users.AdminUserError) -> HTMLResponse:
    # 200 so htmx swaps the message into the action slot.
    return _render(request, "partials/admin_user_error.html", {"error": exc.message})


def _password_reveal(
    request: Request, heading: str, user: users.AdminUser, password: str | None
) -> HTMLResponse:
    response = _render(
        request,
        "partials/admin_user_password.html",
        {"heading": heading, "user": user, "password": password},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/dashboard/admin-users", response_class=HTMLResponse)
async def admin_users_page(request: Request) -> HTMLResponse:
    """Console users list."""
    ctx = {
        "active_page": "admin-users",
        "users": await users.list_admin_users(request.app.state.pg_pool),
        "current_admin_id": users.actor_id(request),
    }
    return _render_page(request, "pages/admin_users.html", "pages/admin_users.html", ctx)


@router.get("/dashboard/admin-users/new", response_class=HTMLResponse)
async def new_admin_user_form(request: Request) -> HTMLResponse:
    """New console user form."""
    ctx = _form_context(request, "new", dict(_EMPTY_FORM))
    return _render_page(request, "pages/admin_user_form.html", "partials/admin_user_form.html", ctx)


@router.post("/dashboard/admin-users/create", response_class=HTMLResponse)
async def create_admin_user_form(request: Request) -> HTMLResponse:
    """Create a console user and show its temporary password once."""
    form = await _posted(request)
    try:
        user, password = await users.create_admin_user(
            request.app.state.pg_pool,
            username=form["username"],
            user_type=form["user_type"],
            custom_roles=form["roles"],
            email=form["email"],
            sso_subject=form["sso_subject"],
            sso_enabled=users.sso_enabled(request),
        )
    except users.AdminUserError as exc:
        return _render(
            request,
            "partials/admin_user_form.html",
            _form_context(request, "new", form, exc.message),
        )
    await users.audit_change(request, action="create", user=user, status_code=201)
    return _password_reveal(request, "Console user created", user, password)


@router.get("/dashboard/admin-users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail(request: Request, user_id: int) -> HTMLResponse:
    """Console user detail."""
    try:
        user = await users.get_admin_user(request.app.state.pg_pool, user_id)
    except users.AdminUserError:
        return _render_page(
            request,
            "pages/not_found.html",
            "pages/not_found.html",
            {"active_page": "admin-users", "what": "Console user"},
        )
    ctx = {
        "active_page": "admin-users",
        "user": user,
        "is_self": user.id == users.actor_id(request),
    }
    return _render_page(
        request, "pages/admin_user_detail.html", "pages/admin_user_detail.html", ctx
    )


@router.get("/dashboard/admin-users/{user_id}/edit", response_class=HTMLResponse)
async def edit_admin_user_form(request: Request, user_id: int) -> HTMLResponse:
    """Edit console user form."""
    try:
        user = await users.get_admin_user(request.app.state.pg_pool, user_id)
    except users.AdminUserError:
        return await admin_user_detail(request, user_id)
    form = {
        "id": user.id,
        "username": user.username,
        "user_type": user.user_type,
        "roles": [role for role in user.roles],
        "email": user.email or "",
        "sso_subject": user.sso_subject or "",
    }
    ctx = _form_context(request, "edit", form)
    return _render_page(request, "pages/admin_user_form.html", "partials/admin_user_form.html", ctx)


@router.post("/dashboard/admin-users/{user_id}/update", response_class=HTMLResponse)
async def update_admin_user_form(request: Request, user_id: int) -> Response:
    """Save a console user's type, roles, email and SSO subject."""
    form = await _posted(request)
    try:
        before, after = await users.update_admin_user(
            request.app.state.pg_pool,
            actor_id=users.actor_id(request),
            user_id=user_id,
            user_type=form["user_type"],
            custom_roles=form["roles"],
            email=form["email"],
            sso_subject=form["sso_subject"],
            sso_enabled=users.sso_enabled(request),
        )
    except users.AdminUserError as exc:
        try:
            current = await users.get_admin_user(request.app.state.pg_pool, user_id)
            form["username"] = current.username
        except users.AdminUserError:
            pass
        form["id"] = user_id
        return _render(
            request,
            "partials/admin_user_form.html",
            _form_context(request, "edit", form, exc.message),
        )
    await users.audit_change(
        request,
        action="update",
        user=after,
        before=before,
        changed_fields=users.changed(before, after),
    )
    return _mutation_redirect(request, f"/dashboard/admin-users/{user_id}")


@router.post("/dashboard/admin-users/{user_id}/reset-password", response_class=HTMLResponse)
async def reset_admin_user_password(request: Request, user_id: int) -> HTMLResponse:
    """Give a console user a new temporary password."""
    try:
        user, password = await users.reset_password(
            request.app.state.pg_pool, actor_id=users.actor_id(request), user_id=user_id
        )
    except users.AdminUserError as exc:
        return _error(request, exc)
    await users.audit_change(request, action="reset_password", user=user)
    return _password_reveal(request, "Password reset", user, password)


async def _set_enabled(request: Request, user_id: int, enabled: bool) -> Response:
    try:
        before, after = await users.set_enabled(
            request.app.state.pg_pool,
            actor_id=users.actor_id(request),
            user_id=user_id,
            enabled=enabled,
        )
    except users.AdminUserError as exc:
        return _error(request, exc)
    await users.audit_change(
        request,
        action="enable" if enabled else "disable",
        user=after,
        before=before,
        changed_fields=users.changed(before, after),
    )
    return _mutation_redirect(request, f"/dashboard/admin-users/{user_id}")


@router.post("/dashboard/admin-users/{user_id}/disable", response_class=HTMLResponse)
async def disable_admin_user(request: Request, user_id: int) -> Response:
    """Disable a console user."""
    return await _set_enabled(request, user_id, False)


@router.post("/dashboard/admin-users/{user_id}/enable", response_class=HTMLResponse)
async def enable_admin_user(request: Request, user_id: int) -> Response:
    """Enable a console user."""
    return await _set_enabled(request, user_id, True)


@router.post("/dashboard/admin-users/{user_id}/delete", response_class=HTMLResponse)
async def delete_admin_user(request: Request, user_id: int) -> Response:
    """Delete a disabled console user."""
    try:
        before = await users.delete_admin_user(
            request.app.state.pg_pool, actor_id=users.actor_id(request), user_id=user_id
        )
    except users.AdminUserError as exc:
        return _error(request, exc)
    await users.audit_change(request, action="delete", user=before, before=before, status_code=303)
    return _mutation_redirect(request, "/dashboard/admin-users")
