"""The event a notifier receives, built from a queue row."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from interlock.security.approval_redaction import statement_fingerprint

ApprovalEventKind = Literal["pending", "approved", "rejected", "expired", "failed"]


@dataclass(frozen=True)
class ApprovalEvent:
    """One thing that happened to an approval, already safe to send.

    The fingerprint is computed at construction rather than at send time so
    that a raw statement never reaches a sender, a payload builder, or a log
    line, whatever a future transport does with the event.
    """

    kind: ApprovalEventKind
    approval_id: int
    source_id: str
    identity_id: int
    risk_level: str
    statement_fingerprint: str
    identity_name: str | None = None
    operation: str | None = None
    protocol: str | None = None
    expires_at: datetime | None = None
    actor: str | None = None
    executed: bool | None = None
    # Exception class name only. Upstream database errors routinely echo the
    # values that caused them.
    failure_kind: str | None = None


def event_from_row(
    kind: ApprovalEventKind,
    row: dict[str, Any],
    *,
    expires_at: datetime | None = None,
    actor: str | None = None,
    executed: bool | None = None,
    failure_kind: str | None = None,
) -> ApprovalEvent:
    """Build an event from a `write_approval_queue` row or an equivalent dict."""
    metadata = row.get("request_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return ApprovalEvent(
        kind=kind,
        approval_id=int(row["id"]),
        source_id=str(row.get("source_id") or ""),
        identity_id=int(row.get("identity_id") or 0),
        risk_level=str(row.get("risk_level") or "unknown"),
        statement_fingerprint=statement_fingerprint(str(row.get("sql_text") or "")),
        identity_name=metadata.get("identity_name"),
        operation=metadata.get("normalized_operation") or metadata.get("method"),
        protocol=metadata.get("protocol"),
        expires_at=expires_at if expires_at is not None else row.get("expires_at"),
        actor=actor,
        executed=executed,
        failure_kind=failure_kind,
    )
