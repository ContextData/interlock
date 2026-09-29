"""Admin authentication: password hashing, session cookies, CSRF tokens.

AUDIT-COVERS: P0-F (admin auth gap) and SR-7/SR-8 (CSRF + secure cookies).

Design notes:

- Passwords are stored using ``hashlib.scrypt`` (stdlib, memory-hard).
  No new dependencies are introduced.
- Session cookies are HMAC-SHA256 signed with the configured secret.
  The cookie payload is ``base64url(json) + "." + base64url(sig)``.
  Server-side state (last-used, revocation) lives in Redis under
  ``admin:session:<sid>``; the cookie carries the session id.
- CSRF tokens are random per-session and stored in Redis under
  ``admin:csrf:<sid>``. The token is delivered via a separate cookie
  (``interlock_admin_csrf``) so HTMX/JS can read it from
  ``document.cookie`` and echo it back in the ``X-CSRF-Token`` header
  for any unsafe (POST/PUT/PATCH/DELETE) request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16

SESSION_REDIS_PREFIX = "admin:session:"
CSRF_REDIS_PREFIX = "admin:csrf:"

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminSession:
    """Resolved session data for the current request."""

    session_id: str
    admin_id: int
    username: str
    roles: tuple[str, ...]
    authorization_version: int
    issued_at: float
    expires_at: float
    # Set for an admin still on a default password: only the change-password
    # page, logout and static assets are reachable until it is changed.
    must_change_password: bool = False


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return a serialized scrypt hash of the password.

    Format: ``scrypt$<N>$<r>$<p>$<salt_hex>$<dk_hex>``
    """
    if not password:
        raise ValueError("password must be non-empty")
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify of a password against a stored scrypt hash."""
    try:
        scheme, n_s, r_s, p_s, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except ValueError:
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
    )
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# Cookie signing
# ---------------------------------------------------------------------------


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_cookie(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64url(raw)
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def verify_cookie(token: str, secret: str) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    body, sig_b64 = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        return json.loads(_b64url_decode(body))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def create_session(
    redis: Redis,
    admin_id: int,
    username: str,
    roles: list[str],
    authorization_version: int,
    ttl_seconds: int,
    must_change_password: bool = False,
) -> tuple[str, str]:
    """Create a session in Redis and return (session_id, csrf_token)."""
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = int(time.time())
    payload = {
        "sid": sid,
        "admin_id": admin_id,
        "username": username,
        "roles": list(roles),
        "authorization_version": authorization_version,
        "must_change_password": must_change_password,
        "issued_at": now,
        "expires_at": now + ttl_seconds,
    }
    await redis.set(
        SESSION_REDIS_PREFIX + sid,
        json.dumps(payload),
        ex=ttl_seconds,
    )
    await redis.set(CSRF_REDIS_PREFIX + sid, csrf, ex=ttl_seconds)
    return sid, csrf


async def resolve_session(redis: Redis, session_id: str) -> AdminSession | None:
    raw = await redis.get(SESSION_REDIS_PREFIX + session_id)
    if not raw:
        return None
    try:
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except Exception:
        return None
    expires_at = float(data.get("expires_at", 0))
    if expires_at < time.time():
        return None
    return AdminSession(
        session_id=session_id,
        admin_id=int(data["admin_id"]),
        username=data["username"],
        roles=tuple(data.get("roles", [])),
        authorization_version=int(data.get("authorization_version", 0)),
        issued_at=float(data.get("issued_at", 0)),
        expires_at=expires_at,
        must_change_password=bool(data.get("must_change_password", False)),
    )


async def invalidate_session(redis: Redis, session_id: str) -> None:
    await redis.delete(
        SESSION_REDIS_PREFIX + session_id,
        CSRF_REDIS_PREFIX + session_id,
    )


async def get_csrf_token(redis: Redis, session_id: str) -> str | None:
    raw = await redis.get(CSRF_REDIS_PREFIX + session_id)
    if raw is None:
        return None
    return raw if isinstance(raw, str) else raw.decode("utf-8")


# ---------------------------------------------------------------------------
# Bootstrapping
# ---------------------------------------------------------------------------


def derive_dev_secret() -> str:
    """Generate an ephemeral signing secret for development.

    Returned value is logged exactly once on boot - sessions invalidate
    on every restart, which is intentional (never run dev secrets in
    production).
    """
    return secrets.token_urlsafe(48)


def env_secret_or_default(config_value: str) -> str:
    if config_value:
        return config_value
    env = os.environ.get("INTERLOCK_ADMIN__SECRET_KEY")
    if env:
        return env
    return derive_dev_secret()
