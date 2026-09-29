"""Shared Admin control-plane audit helpers."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from starlette.requests import Request

from interlock.errors import AuditUnavailableError

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|token|secret|private[_-]?key|api[_-]?key|access[_-]?key|"
    r"credential|session|authorization|cookie|connection[_-]?string|dsn|hash)",
    re.IGNORECASE,
)

_STRICT_MUTATION_PREFIXES = (
    "approval.",
    "data_source.",
    "identity.",
    "password.",
    "policy.",
    "source_role.",
)


def redact_admin_audit_value(value: Any) -> Any:
    """Recursively redact secret-like values before writing Admin audit rows."""
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY_RE.search(str(key))
                else redact_admin_audit_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_admin_audit_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_admin_audit_value(item) for item in value]
    if isinstance(value, str):
        return _redact_secret_like_text(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal, Path, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(value)} bytes>"
    return value


def _redact_secret_like_text(text: str) -> str:
    redacted = re.sub(r"(?i)(authorization|cookie):\s*[^\s,;]+", r"\1: [REDACTED]", text)
    redacted = re.sub(
        r"(?i)(password|token|secret|api_key|private_key)([\"']?\s*[:=]\s*[\"']?)" r"[^\"'\s,}]+",
        r"\1\2[REDACTED]",
        redacted,
    )
    return redacted


def summarize_admin_resource(row: Any, *, fields: tuple[str, ...]) -> dict[str, Any] | None:
    """Return a small redacted summary of a DB row for Admin audit before/after."""
    if row is None:
        return None
    data = dict(row)
    summary = {field: data.get(field) for field in fields if field in data}
    return redact_admin_audit_value(summary)


def mutation_audit_detail(
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    changed_fields: list[str] | tuple[str, ...] | None = None,
    status_code: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    if before is not None:
        detail["before"] = before
    if after is not None:
        detail["after"] = after
    if changed_fields is not None:
        detail["changed_fields"] = sorted(str(field) for field in changed_fields)
    if status_code is not None:
        detail["status_code"] = status_code
    if extra:
        detail.update(extra)
    return redact_admin_audit_value(detail)


async def audit_admin_action(
    request: Request,
    *,
    action: str,
    success: bool,
    resource: str | None = None,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
    error: str | None = None,
    strict: bool | None = None,
) -> None:
    """Persist an Admin audit event.

    Security-sensitive mutations fail closed when their durable audit record
    cannot be accepted. Lower-risk operational actions remain best effort so
    an audit outage does not prevent recovery work. Callers can override the
    inferred behavior explicitly when an action has different semantics.
    """

    if strict is None:
        strict = action.startswith(_STRICT_MUTATION_PREFIXES)
    pg_pool = getattr(request.app.state, "pg_pool", None)
    if pg_pool is None:
        if strict:
            raise AuditUnavailableError("Admin audit database is unavailable")
        return
    admin = getattr(request.state, "admin", None)
    try:
        await pg_pool.execute(
            """
            INSERT INTO admin_audit_log
                (admin_id, username, action, resource, resource_id,
                 detail, request_ip, user_agent, success, error_message)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            getattr(admin, "admin_id", None),
            getattr(admin, "username", None),
            action,
            resource,
            resource_id,
            redact_admin_audit_value(detail or {}),
            request.client.host if request.client else None,
            request.headers.get("user-agent"),
            success,
            error,
        )
    except Exception as exc:
        logger.exception("Failed to write admin audit row (action=%s)", action)
        if strict:
            raise AuditUnavailableError(
                "The control-plane mutation could not be durably audited"
            ) from exc
