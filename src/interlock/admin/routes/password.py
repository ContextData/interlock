"""Change an admin's own password.

Every admin can reach this page from the sidebar. An admin created with the
default password `admin` is sent here after signing in and can reach nothing
else until the password is changed (see `auth_middleware`).

A successful change bumps the admin's `authorization_version` (migration 019),
which revokes every other session of that admin; the session making the change
is re-issued so it carries on.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from interlock.admin.audit import audit_admin_action
from interlock.admin.auth import create_session, hash_password, invalidate_session, verify_password
from interlock.admin.defaults import DEFAULT_ADMIN_PASSWORD
from interlock.admin.routes.auth import (
    CHANGE_PASSWORD_PATH,
    _clear_login_failures,
    _client_ip,
    _record_login_failure,
    _retry_after,
    _set_session_cookies,
)
from interlock.admin.routes.dashboard import _render

logger = logging.getLogger(__name__)
router = APIRouter()

MIN_PASSWORD_LENGTH = 12
_THROTTLE_PREFIX = "admin:password_change_fail"


def password_change_problem(*, username: str, current: str, new: str, confirm: str) -> str | None:
    """Why `new` cannot be the password, or None when it can."""
    if len(new) < MIN_PASSWORD_LENGTH:
        return f"The new password must be at least {MIN_PASSWORD_LENGTH} characters."
    if new != confirm:
        return "The new password and its confirmation do not match."
    lowered = new.casefold()
    if lowered == DEFAULT_ADMIN_PASSWORD or lowered == username.casefold():
        return "The new password cannot be the default password or your username."
    if new == current:
        return "The new password must differ from the current one."
    return None


def _throttle_keys(admin_id: int, client_ip: str) -> tuple[str, str]:
    ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
    return (f"{_THROTTLE_PREFIX}:ip:{ip_hash}", f"{_THROTTLE_PREFIX}:admin:{admin_id}")


def _wants_json(request: Request) -> bool:
    return request.headers.get("hx-request") != "true" and "text/html" not in request.headers.get(
        "accept", ""
    )


def _form_response(request: Request, *, error: str | None, status_code: int) -> Response:
    if _wants_json(request):
        return JSONResponse({"error": error}, status_code=status_code)
    response = _render(
        request,
        "partials/change_password_form.html",
        {"error": error, "forced": _forced(request)},
    )
    # htmx swaps only 2xx responses by default; the form carries the error.
    response.status_code = 200 if request.headers.get("hx-request") == "true" else status_code
    return response


def _forced(request: Request) -> bool:
    return bool(getattr(getattr(request.state, "admin", None), "must_change_password", False))


@router.get(CHANGE_PASSWORD_PATH, response_class=HTMLResponse)
async def change_password_page(request: Request) -> HTMLResponse:
    return _render(
        request,
        "pages/change_password.html",
        {"active_page": "change-password", "error": None, "forced": _forced(request)},
    )


@router.post(CHANGE_PASSWORD_PATH)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    session = request.state.admin
    config = request.app.state.config
    pool = request.app.state.pg_pool
    redis = request.app.state.redis
    keys = _throttle_keys(session.admin_id, _client_ip(request))

    retry_after = await _retry_after(redis, keys)
    if retry_after is not None:
        throttled = _form_response(
            request, error="Too many attempts. Try again shortly.", status_code=429
        )
        throttled.headers["Retry-After"] = str(retry_after)
        return throttled

    row = await pool.fetchrow(
        "SELECT username, password_hash FROM admin_identities WHERE id = $1 AND enabled",
        session.admin_id,
    )
    if row is None or not row["password_hash"]:
        return _form_response(
            request, error="This account has no local password to change.", status_code=400
        )
    if not verify_password(current_password, row["password_hash"]):
        await _record_login_failure(redis, keys)
        await audit_admin_action(
            request,
            action="password.change",
            resource="admin_identity",
            resource_id=str(session.admin_id),
            success=False,
            error="current_password_incorrect",
        )
        return _form_response(request, error="The current password is incorrect.", status_code=400)

    problem = password_change_problem(
        username=row["username"],
        current=current_password,
        new=new_password,
        confirm=confirm_password,
    )
    if problem is not None:
        return _form_response(request, error=problem, status_code=400)

    updated = await pool.fetchrow(
        """
        UPDATE admin_identities
        SET password_hash = $2, must_change_password = FALSE, password_changed_at = NOW()
        WHERE id = $1
        RETURNING username, roles, authorization_version
        """,
        session.admin_id,
        hash_password(new_password),
    )
    await _clear_login_failures(redis, keys)
    await audit_admin_action(
        request,
        action="password.change",
        resource="admin_identity",
        resource_id=str(session.admin_id),
        success=True,
        detail={"was_forced": bool(session.must_change_password)},
    )

    # The trigger bumped authorization_version, which revokes every session of
    # this admin, this one included. Re-issue this one so the admin carries on.
    await invalidate_session(redis, session.session_id)
    sid, csrf = await create_session(
        redis,
        admin_id=session.admin_id,
        username=updated["username"],
        roles=list(updated["roles"] or []),
        authorization_version=int(updated["authorization_version"]),
        ttl_seconds=config.admin.session_ttl_seconds,
    )
    response: Response
    if _wants_json(request):
        response = JSONResponse({"changed": True})
    else:
        response = Response(status_code=200, headers={"HX-Redirect": "/dashboard/overview"})
    _set_session_cookies(
        response,
        secret_key=request.app.state.admin_secret_key,
        cookie_name=config.admin.cookie_name,
        csrf_cookie_name=config.admin.csrf_cookie_name,
        cookie_secure=config.admin.cookie_secure,
        sid=sid,
        csrf=csrf,
        ttl_seconds=config.admin.session_ttl_seconds,
    )
    return response
