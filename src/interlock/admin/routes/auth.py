"""Admin auth endpoints: login, logout, CSRF token issuance.

AUDIT-COVERS: P0-F, SR-7, SR-8

The login route accepts either form-encoded POST (browser path,
returning HTML) or JSON (programmatic clients). It verifies the
password against ``admin_identities`` and creates a Redis-backed session
plus a CSRF token. Both the signed session cookie and the CSRF cookie
are returned with the response.

Admin actions are written to ``admin_audit_log`` so we have a trail of
who did what regardless of data-plane audit.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from interlock.admin.auth import (
    create_session,
    invalidate_session,
    sign_cookie,
    verify_cookie,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter()

CHANGE_PASSWORD_PATH = "/auth/change-password"

_LOGIN_FAILURE_LIMIT = 5
_LOGIN_THROTTLE_WINDOW_SECONDS = 300
_LOGIN_THROTTLE_PREFIX = "admin:login_fail"
_OIDC_FLOW_PREFIX = "admin:oidc:flow:"
_OIDC_FLOW_COOKIE = "interlock_oidc_flow"
_ADMIN_ROLE_ALLOWLIST = frozenset(
    {
        "owner",
        "admin",
        "security_admin",
        "source_admin",
        "policy_admin",
        "approval_reviewer",
        "auditor",
    }
)

_OIDC_GETDEL_LUA = """
local value = redis.call('GET', KEYS[1])
if value then redis.call('DEL', KEYS[1]) end
return value
"""


# Login page HTML. Styles live in /static/css/dashboard.css so they
# survive the SecurityHeadersMiddleware CSP (which blocks inline
# <style> blocks). Keep this template self-contained (no Jinja base
# extends) so the page renders even if the templates engine is wedged.
_LOGIN_HTML = """<!doctype html>
<html lang="en"><head>
  <title>InterLock Admin Login</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" href="/static/assets/logo/favicon-64.png">
  <link rel="stylesheet" href="/static/css/dashboard.css">
</head>
<body class="login-body">
  <main class="login-card">
    <img src="/static/assets/logo/context-data-lockup.svg" alt="context.data" class="login-logo logo-dark">
    <img src="/static/assets/logo/context-data-lockup-light.svg" alt="" aria-hidden="true" class="login-logo logo-light">
    <h1>Sign in</h1>
    <p class="login-lede">InterLock governance console</p>
    __SSO__
    __LOCAL_FORM__
    __AUTH_MODE__
    __ERROR__
  </main>
</body></html>
"""

_LOCAL_LOGIN_FORM = """
    <form method="post" action="/auth/login">
    <label for="lf-user">Username</label>
    <input id="lf-user" type="text" name="username" autofocus required>
    <label for="lf-pass">Password</label>
    <input id="lf-pass" type="password" name="password" required>
    <button type="submit">Sign in</button>
    </form>
"""


def _login_html(
    error: str = "",
    auth_mode: str = "",
    *,
    oidc_enabled: bool = False,
    local_enabled: bool = True,
) -> str:
    auth_mode_html = (
        f'<p class="login-mode">{auth_mode} authentication is enabled.</p>' if auth_mode else ""
    )
    error_html = f'<div class="login-err">{error}</div>' if error else ""
    sso_html = (
        '<a class="btn btn-primary login-sso" href="/auth/oidc/login">Sign in with SSO</a>'
        if oidc_enabled
        else ""
    )
    return (
        _LOGIN_HTML.replace("__SSO__", sso_html)
        .replace("__LOCAL_FORM__", _LOCAL_LOGIN_FORM if local_enabled else "")
        .replace("__AUTH_MODE__", auth_mode_html)
        .replace("__ERROR__", error_html)
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_throttle_keys(username: str, client_ip: str) -> tuple[str, str]:
    normalized_user = username.strip().casefold() or "unknown"
    user_hash = hashlib.sha256(normalized_user.encode("utf-8")).hexdigest()
    ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
    return (
        f"{_LOGIN_THROTTLE_PREFIX}:ip:{ip_hash}",
        f"{_LOGIN_THROTTLE_PREFIX}:user:{user_hash}",
    )


async def _redis_call(redis: Any, command: str, *args: Any) -> Any:
    func = getattr(redis, command, None)
    if not callable(func):
        raise HTTPException(status_code=503, detail="auth_throttle_unavailable")
    result = func(*args)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _consume_oidc_flow(redis: Any, key: str) -> Any:
    """Atomically consume one OIDC flow, including pre-6.2 Redis servers."""
    getdel = getattr(redis, "getdel", None)
    if callable(getdel):
        try:
            result = getdel(key)
            return await result if inspect.isawaitable(result) else result
        except Exception:
            logger.info("Redis GETDEL unavailable; using atomic Lua compatibility path")
    eval_command = getattr(redis, "eval", None)
    if not callable(eval_command):
        raise HTTPException(status_code=503, detail="oidc_flow_store_unavailable")
    try:
        result = eval_command(_OIDC_GETDEL_LUA, 1, key)
        return await result if inspect.isawaitable(result) else result
    except Exception as exc:
        raise HTTPException(status_code=503, detail="oidc_flow_store_unavailable") from exc


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _retry_after(redis: Any, keys: tuple[str, str]) -> int | None:
    retry_after: int | None = None
    for key in keys:
        count = _as_int(await _redis_call(redis, "get", key))
        if count < _LOGIN_FAILURE_LIMIT:
            continue
        ttl = _as_int(await _redis_call(redis, "ttl", key), _LOGIN_THROTTLE_WINDOW_SECONDS)
        retry_after = max(retry_after or 0, ttl if ttl > 0 else _LOGIN_THROTTLE_WINDOW_SECONDS)
    return retry_after


async def _record_login_failure(redis: Any, keys: tuple[str, str]) -> None:
    for key in keys:
        count = _as_int(await _redis_call(redis, "incr", key))
        if count == 1:
            await _redis_call(redis, "expire", key, _LOGIN_THROTTLE_WINDOW_SECONDS)


async def _clear_login_failures(redis: Any, keys: tuple[str, str]) -> None:
    await _redis_call(redis, "delete", *keys)


def _rate_limited_response(request: Request, retry_after: int) -> Response:
    headers = {"Retry-After": str(retry_after)}
    if request.headers.get("accept", "").find("text/html") >= 0:
        html = _login_html("Too many sign-in attempts. Try again shortly.")
        return HTMLResponse(html, status_code=429, headers=headers)
    return JSONResponse(
        {"detail": "rate_limited", "retry_after_seconds": retry_after},
        status_code=429,
        headers=headers,
    )


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, reason: str = "") -> HTMLResponse:
    config = request.app.state.config
    oidc_enabled = bool(config.auth.oidc.enabled)
    mode = "OIDC" if oidc_enabled else ""
    messages = {
        "expired": "Your session expired. Sign in again.",
        "locked": "This admin account is locked or disabled.",
        "rate-limit": "Too many sign-in attempts. Try again shortly.",
        "oidc-error": "Single sign-on could not be completed.",
    }
    return HTMLResponse(
        _login_html(
            messages.get(reason, ""),
            mode,
            oidc_enabled=oidc_enabled,
            local_enabled=(not oidc_enabled or config.auth.oidc.local_break_glass_enabled),
        )
    )


@router.get("/auth/oidc/login")
async def oidc_login(request: Request) -> Response:
    """Start a one-time, Redis-backed OIDC authorization flow."""
    config = request.app.state.config
    provider = getattr(request.app.state, "oidc_provider", None)
    if not config.auth.oidc.enabled or provider is None:
        raise HTTPException(status_code=404, detail="oidc_not_enabled")

    flow_id = secrets.token_urlsafe(32)
    state = provider.make_state()
    nonce = secrets.token_urlsafe(32)
    code_verifier, code_challenge = provider.make_pkce_pair()
    flow = json.dumps(
        {"state": state, "nonce": nonce, "code_verifier": code_verifier},
        separators=(",", ":"),
    )
    set_result = request.app.state.redis.set(
        _OIDC_FLOW_PREFIX + flow_id,
        flow,
        ex=config.auth.oidc.flow_ttl_seconds,
    )
    if inspect.isawaitable(set_result):
        await set_result
    response = RedirectResponse(
        provider.get_authorization_url(
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
        ),
        status_code=302,
    )
    response.set_cookie(
        _OIDC_FLOW_COOKIE,
        sign_cookie({"flow_id": flow_id}, request.app.state.admin_secret_key),
        max_age=config.auth.oidc.flow_ttl_seconds,
        httponly=True,
        secure=config.admin.cookie_secure,
        samesite="lax",
        path="/auth/oidc",
    )
    return response


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, code: str = "", state: str = "") -> Response:
    """Complete OIDC login for an explicitly pre-provisioned Admin."""
    config = request.app.state.config
    provider = getattr(request.app.state, "oidc_provider", None)
    signed_flow = request.cookies.get(_OIDC_FLOW_COOKIE, "")
    flow_cookie = verify_cookie(signed_flow, request.app.state.admin_secret_key)
    flow_id = str((flow_cookie or {}).get("flow_id") or "")
    if not config.auth.oidc.enabled or provider is None or not flow_id or not code or not state:
        return RedirectResponse("/auth/login?reason=oidc-error", status_code=302)

    redis = request.app.state.redis
    raw_flow = await _consume_oidc_flow(redis, _OIDC_FLOW_PREFIX + flow_id)
    try:
        if isinstance(raw_flow, bytes):
            raw_flow = raw_flow.decode("utf-8")
        flow = json.loads(raw_flow or "{}")
        if not provider.verify_state(state, str(flow.get("state") or "")):
            raise ValueError("state mismatch")
        code_verifier = str(flow.get("code_verifier") or "")
        if not code_verifier:
            raise ValueError("missing PKCE verifier")
        tokens = await provider.exchange_code(code, code_verifier=code_verifier)
        if not tokens.id_token:
            raise ValueError("missing id_token")
        user_info = await provider.verify_token(
            tokens.id_token,
            nonce=str(flow.get("nonce") or ""),
        )
    except Exception:
        logger.warning("OIDC callback validation failed", exc_info=True)
        await _audit(request, action="oidc_login", success=False, error="verification_failed")
        response = RedirectResponse("/auth/login?reason=oidc-error", status_code=302)
        response.delete_cookie(_OIDC_FLOW_COOKIE, path="/auth/oidc")
        return response

    row = await request.app.state.pg_pool.fetchrow(
        """
        SELECT id, username, roles, enabled, authorization_version
        FROM admin_identities
        WHERE oidc_subject = $1
        LIMIT 1
        """,
        user_info.sub,
    )
    if row is None or not row["enabled"]:
        await _audit(
            request,
            action="oidc_login",
            success=False,
            username=user_info.email or user_info.sub,
            error="identity_not_provisioned",
        )
        response = RedirectResponse("/auth/login?reason=locked", status_code=302)
        response.delete_cookie(_OIDC_FLOW_COOKIE, path="/auth/oidc")
        return response

    roles = {str(role).strip().lower() for role in (row["roles"] or [])}
    for group in user_info.groups:
        roles.update(config.auth.oidc.admin_group_role_map.get(group, []))
    safe_roles = sorted(roles & _ADMIN_ROLE_ALLOWLIST)
    sid, csrf = await create_session(
        redis,
        admin_id=row["id"],
        username=row["username"],
        roles=safe_roles,
        authorization_version=int(row["authorization_version"]),
        ttl_seconds=config.admin.session_ttl_seconds,
    )
    await request.app.state.pg_pool.execute(
        """
        UPDATE admin_identities
        SET last_login_at = NOW(), last_login_ip = $2, email = COALESCE($3, email)
        WHERE id = $1
        """,
        row["id"],
        _client_ip(request),
        user_info.email,
    )
    response = RedirectResponse("/dashboard/overview", status_code=302)
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
    response.delete_cookie(_OIDC_FLOW_COOKIE, path="/auth/oidc")
    await _audit(
        request,
        action="oidc_login",
        success=True,
        username=row["username"],
        admin_id=row["id"],
    )
    return response


def _set_session_cookies(
    response: Response,
    *,
    secret_key: str,
    cookie_name: str,
    csrf_cookie_name: str,
    cookie_secure: bool,
    sid: str,
    csrf: str,
    ttl_seconds: int,
) -> None:
    cookie_value = sign_cookie({"sid": sid}, secret_key)
    response.set_cookie(
        key=cookie_name,
        value=cookie_value,
        max_age=ttl_seconds,
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=csrf_cookie_name,
        value=csrf,
        max_age=ttl_seconds,
        httponly=False,  # Readable by HTMX/JS so it can be echoed in header.
        secure=cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/auth/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    config = request.app.state.config
    pg_pool = request.app.state.pg_pool
    redis = request.app.state.redis
    secret_key = request.app.state.admin_secret_key
    throttle_keys = _login_throttle_keys(username, _client_ip(request))
    if config.auth.oidc.enabled and not config.auth.oidc.local_break_glass_enabled:
        await _audit(
            request, action="login", success=False, username=username, error="sso_required"
        )
        raise HTTPException(status_code=403, detail="sso_required")
    retry_after = await _retry_after(redis, throttle_keys)
    if retry_after is not None:
        await _audit(
            request,
            action="login",
            success=False,
            username=username,
            error="rate_limited",
        )
        return _rate_limited_response(request, retry_after)

    row = await pg_pool.fetchrow(
        """
        SELECT id, username, password_hash, roles, enabled, authorization_version,
               must_change_password
        FROM admin_identities WHERE username = $1
        """,
        username,
    )
    if (
        row is None
        or not row["enabled"]
        or not row["password_hash"]
        or not verify_password(password, row["password_hash"])
    ):
        await _record_login_failure(redis, throttle_keys)
        await _audit(
            request, action="login", success=False, username=username, error="invalid_credentials"
        )
        # Constant-ish error to deter user enumeration.
        if request.headers.get("accept", "").find("text/html") >= 0:
            html = _login_html("Invalid username or password.")
            return HTMLResponse(html, status_code=401)
        raise HTTPException(status_code=401, detail="invalid_credentials")

    roles = list(row["roles"] or [])
    must_change = bool(row.get("must_change_password") or False)
    if config.auth.oidc.enabled and not ({"owner", "admin"} & {r.lower() for r in roles}):
        await _audit(
            request,
            action="login",
            success=False,
            username=username,
            admin_id=row["id"],
            error="break_glass_owner_required",
        )
        raise HTTPException(status_code=403, detail="break_glass_owner_required")
    sid, csrf = await create_session(
        redis,
        admin_id=row["id"],
        username=row["username"],
        roles=roles,
        authorization_version=int(row["authorization_version"]),
        ttl_seconds=config.admin.session_ttl_seconds,
        must_change_password=must_change,
    )
    await _clear_login_failures(redis, throttle_keys)

    # Update last-login bookkeeping.
    await pg_pool.execute(
        """
        UPDATE admin_identities
        SET last_login_at = NOW(), last_login_ip = $2
        WHERE id = $1
        """,
        row["id"],
        request.client.host if request.client else None,
    )

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        landing = CHANGE_PASSWORD_PATH if must_change else "/dashboard/overview"
        resp: Response = RedirectResponse(url=landing, status_code=302)
    else:
        body: dict[str, Any] = {"username": row["username"], "roles": roles}
        if must_change:
            body["password_change_required"] = True
        resp = JSONResponse(body)

    _set_session_cookies(
        resp,
        secret_key=secret_key,
        cookie_name=config.admin.cookie_name,
        csrf_cookie_name=config.admin.csrf_cookie_name,
        cookie_secure=config.admin.cookie_secure,
        sid=sid,
        csrf=csrf,
        ttl_seconds=config.admin.session_ttl_seconds,
    )
    await _audit(request, action="login", success=True, username=username, admin_id=row["id"])
    return resp


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    config = request.app.state.config
    redis = request.app.state.redis
    secret_key = request.app.state.admin_secret_key

    sid = None
    cookie_token = request.cookies.get(config.admin.cookie_name)
    if cookie_token:
        payload = verify_cookie(cookie_token, secret_key)
        if payload:
            sid = payload.get("sid")
    if sid:
        await invalidate_session(redis, sid)

    resp: Response = RedirectResponse(url="/auth/login", status_code=302)
    resp.delete_cookie(config.admin.cookie_name, path="/")
    resp.delete_cookie(config.admin.csrf_cookie_name, path="/")
    resp.delete_cookie(_OIDC_FLOW_COOKIE, path="/auth/oidc")
    return resp


@router.get("/auth/csrf")
async def csrf(request: Request) -> JSONResponse:
    """Return the CSRF token for the current session (auth required)."""
    config = request.app.state.config
    redis = request.app.state.redis
    secret_key = request.app.state.admin_secret_key

    cookie_token = request.cookies.get(config.admin.cookie_name)
    payload = verify_cookie(cookie_token or "", secret_key)
    if not payload:
        raise HTTPException(status_code=401, detail="not_authenticated")

    from interlock.admin.auth import get_csrf_token, resolve_session

    sid = payload.get("sid")
    if not sid:
        raise HTTPException(status_code=401, detail="not_authenticated")
    session = await resolve_session(redis, sid)
    if session is None:
        raise HTTPException(status_code=401, detail="session_expired")
    token = await get_csrf_token(redis, sid)
    if token is None:
        raise HTTPException(status_code=401, detail="session_expired")
    return JSONResponse({"csrf": token})


# ---------------------------------------------------------------------------
# Admin audit helper
# ---------------------------------------------------------------------------


async def _audit(
    request: Request,
    *,
    action: str,
    success: bool,
    admin_id: int | None = None,
    username: str | None = None,
    resource: str | None = None,
    resource_id: str | None = None,
    detail: dict[str, object] | None = None,
    error: str | None = None,
) -> None:
    pg_pool = getattr(request.app.state, "pg_pool", None)
    if pg_pool is None:
        return
    try:
        await pg_pool.execute(
            """
            INSERT INTO admin_audit_log
                (admin_id, username, action, resource, resource_id,
                 detail, request_ip, user_agent, success, error_message)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            admin_id,
            username,
            action,
            resource,
            resource_id,
            detail,
            request.client.host if request.client else None,
            request.headers.get("user-agent"),
            success,
            error,
        )
    except Exception:
        logger.exception("Failed to write admin audit row (action=%s)", action)
