"""Control-plane audit failure semantics for public-beta mutations."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from interlock.admin.audit import audit_admin_action, redact_admin_audit_value
from interlock.errors import AuditUnavailableError


def _request(pool: AsyncMock | None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(pg_pool=pool)),
        state=SimpleNamespace(admin=SimpleNamespace(admin_id=7, username="security-admin")),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"user-agent": "test"},
    )


@pytest.mark.asyncio
async def test_high_risk_admin_mutation_fails_closed_when_audit_is_unavailable() -> None:
    pool = AsyncMock()
    pool.execute.side_effect = RuntimeError("database offline")

    with pytest.raises(AuditUnavailableError):
        await audit_admin_action(
            _request(pool),
            action="identity.api_key.rotate",
            success=True,
            resource="identity",
            resource_id="42",
        )


@pytest.mark.asyncio
async def test_low_risk_admin_operation_can_degrade_when_audit_is_unavailable() -> None:
    pool = AsyncMock()
    pool.execute.side_effect = RuntimeError("database offline")

    await audit_admin_action(
        _request(pool),
        action="ingestion.worker.health_probe",
        success=False,
        strict=False,
    )


@pytest.mark.asyncio
async def test_high_risk_admin_mutation_requires_an_audit_pool() -> None:
    with pytest.raises(AuditUnavailableError):
        await audit_admin_action(
            _request(None),
            action="policy.update",
            success=True,
        )


def test_admin_audit_detail_is_json_serializable_for_database_native_values() -> None:
    detail = redact_admin_audit_value(
        {
            "created_at": datetime(2026, 7, 17, 12, 30, tzinfo=UTC),
            "approval_id": UUID("12345678-1234-5678-1234-567812345678"),
            "cost": Decimal("1.25"),
            "payload": b"opaque",
        }
    )

    encoded = json.dumps(detail)

    assert "2026-07-17T12:30:00+00:00" in encoded
    assert "12345678-1234-5678-1234-567812345678" in encoded
    assert '"1.25"' in encoded
    assert "<binary:6 bytes>" in encoded
