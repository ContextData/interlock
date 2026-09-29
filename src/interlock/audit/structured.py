"""Structured audit logging helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

from interlock.models import AuditRecord


def log_audit_persisted(
    logger: logging.Logger,
    record: AuditRecord,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a safe structured log for a persisted audit row.

    The log intentionally includes routing/correlation fields only. Request
    bodies, headers, and arbitrary metadata are kept out of logs so audit/event
    details remain the single controlled place for payload inspection.
    """
    logger.info(
        "audit event persisted",
        extra=_audit_log_extra(record, metadata=metadata),
    )


def _audit_log_extra(
    record: AuditRecord,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_metadata = (
        metadata if metadata is not None else _metadata_object(record.request_metadata)
    )
    return {
        "audit_event_id": str(resolved_metadata.get("event_id") or ""),
        "audit_correlation_id": str(resolved_metadata.get("correlation_id") or ""),
        "audit_identity_id": record.identity_id,
        "audit_source_id": record.source_id,
        "audit_protocol": record.protocol or resolved_metadata.get("protocol"),
        "audit_operation": record.operation,
        "audit_status": record.status,
        "audit_route": record.route
        or resolved_metadata.get("route")
        or resolved_metadata.get("path"),
        "audit_approval_id": record.approval_id or resolved_metadata.get("approval_id"),
    }


def _metadata_object(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"raw": raw}
    return {"value": raw}
