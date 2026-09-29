"""Reusable assertions for compose-backed E2E tests."""

from __future__ import annotations

import json
from typing import Any

import asyncpg


def json_metadata(row: asyncpg.Record, column: str = "request_metadata") -> dict[str, Any]:
    value: Any = row[column]
    for _ in range(3):
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            value = json.loads(value)
            continue
        return dict(value)
    return {}


def assert_audit_event(
    row: asyncpg.Record | None,
    *,
    source_id: str,
    protocol: str | None = None,
    status: str | None = None,
) -> asyncpg.Record:
    assert row is not None, "expected an audit row"
    assert row["source_id"] == source_id
    if status is not None:
        assert row["status"] == status
    if protocol is not None:
        metadata = json_metadata(row)
        assert metadata.get("protocol") == protocol
    return row


def assert_approval_state(
    row: asyncpg.Record | None,
    *,
    source_id: str,
    status: str,
) -> asyncpg.Record:
    assert row is not None, "expected an approval row"
    assert row["source_id"] == source_id
    assert row["status"] == status
    return row


def assert_interlock_auth_not_forwarded(calls: dict[str, Any]) -> None:
    for call in calls.get("calls", []):
        assert call.get("authorization") in (
            None,
            "",
        ), "InterLock Authorization header leaked to HTTP upstream"
