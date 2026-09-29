"""Tests for the shared GatewayPipeline governance spine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.errors import AuditUnavailableError, WriteApprovalRequiredError
from interlock.gateway.pipeline import GatewayPipeline, GatewayRequest
from interlock.models import (
    AgentType,
    IdentityContext,
    PolicyDecision,
    RateLimitResult,
    RiskLevel,
    WriteClassification,
)


def _identity() -> IdentityContext:
    return IdentityContext(
        identity_id=7,
        user="agent",
        agent_type=AgentType.CUSTOM,
        team="eng",
        roles=["reader"],
        session_id="sess-7",
    )


@pytest.mark.asyncio
async def test_strict_audit_unavailable_propagates_from_pipeline() -> None:
    audit = MagicMock()
    audit.log = AsyncMock(side_effect=AuditUnavailableError("durable sink unavailable"))
    pipeline = GatewayPipeline(audit_logger=audit)

    with pytest.raises(AuditUnavailableError, match="durable sink unavailable"):
        await pipeline.audit(GatewayRequest(protocol="http", source_id="s1", operation="read"))


@pytest.mark.asyncio
async def test_non_durability_audit_error_remains_best_effort() -> None:
    audit = MagicMock()
    audit.log = AsyncMock(side_effect=RuntimeError("logger implementation bug"))
    pipeline = GatewayPipeline(audit_logger=audit)

    await pipeline.audit(GatewayRequest(protocol="http", source_id="s1", operation="read"))


@pytest.mark.asyncio
async def test_pipeline_authenticates_bearer_and_allows_policy() -> None:
    identity = _identity()
    auth = MagicMock()
    auth.authenticate = AsyncMock(return_value=identity)
    policy = MagicMock()
    policy.evaluate.return_value = PolicyDecision(allowed=True, reason="ok")

    pipeline = GatewayPipeline(auth_manager=auth, policy_engine=policy)
    decision = await pipeline.preflight(
        GatewayRequest(protocol="http", source_id="s1", operation="read"),
        bearer_token="key",
    )

    assert decision.allowed is True
    assert decision.identity == identity
    auth.authenticate.assert_awaited_once_with("key")
    policy.evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_denies_missing_bearer_when_auth_configured() -> None:
    pipeline = GatewayPipeline(auth_manager=MagicMock())

    decision = await pipeline.preflight(
        GatewayRequest(protocol="mcp", source_id="s1", operation="read")
    )

    assert decision.allowed is False
    assert decision.status_code == 401


@pytest.mark.asyncio
async def test_pipeline_denies_policy_and_preserves_explanation() -> None:
    policy = MagicMock()
    policy.evaluate.return_value = PolicyDecision(
        allowed=False,
        reason="default deny",
    )

    pipeline = GatewayPipeline(policy_engine=policy)
    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="postgresql",
            source_id="s1",
            operation="write",
            identity=_identity(),
            tables=["users"],
        )
    )

    assert decision.allowed is False
    assert decision.status_code == 403
    assert "default deny" in decision.reason


@pytest.mark.asyncio
async def test_pipeline_applies_rate_limit_after_policy() -> None:
    rate_limiter = MagicMock()
    rate_limiter.check_multi = AsyncMock(
        return_value=RateLimitResult(
            allowed=False,
            remaining=0,
            limit=1,
            reset_at=123.0,
        )
    )
    policy = MagicMock()
    policy.evaluate.return_value = PolicyDecision(allowed=True, reason="ok")

    pipeline = GatewayPipeline(
        rate_limiter=rate_limiter,
        policy_engine=policy,
    )
    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="postgresql",
            source_id="s1",
            operation="read",
            identity=_identity(),
        )
    )

    assert decision.allowed is False
    assert decision.status_code == 429
    policy.evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_policy_rate_limit_overrides_default_limit() -> None:
    rate_limiter = MagicMock()
    rate_limiter.check_multi = AsyncMock(
        return_value=RateLimitResult(
            allowed=True,
            remaining=1,
            limit=2,
            reset_at=123.0,
        )
    )
    policy = MagicMock()
    policy.evaluate.return_value = PolicyDecision(
        allowed=True,
        reason="ok",
        rate_limit={"limit": 2, "window_seconds": 30},
    )

    pipeline = GatewayPipeline(rate_limiter=rate_limiter, policy_engine=policy)
    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="http",
            source_id="api",
            operation="read",
            identity=_identity(),
        )
    )

    assert decision.allowed is True
    assert rate_limiter.check_multi.await_args.kwargs["limits"] == {
        "ratelimit:policy:api:user:7": (2, 30)
    }


@pytest.mark.asyncio
async def test_pipeline_passes_classification_tags_to_policy() -> None:
    policy = MagicMock()
    policy.evaluate.return_value = PolicyDecision(allowed=True, reason="ok")
    pipeline = GatewayPipeline(policy_engine=policy)

    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="http",
            source_id="api",
            operation="read",
            identity=_identity(),
            metadata={"classification_tags": ["pii", "restricted"]},
        )
    )

    assert decision.allowed is True
    assert policy.evaluate.call_args.kwargs["classification_tags"] == ["pii", "restricted"]


@pytest.mark.asyncio
async def test_pipeline_audit_metadata_marks_legacy_source_role_presence() -> None:
    policy = MagicMock()
    policy.evaluate.return_value = PolicyDecision(allowed=True, reason="ok")
    identity = _identity()
    identity.source_roles = [
        {
            "source_id": "api",
            "role": "legacy-reader",
            "authority": "legacy_metadata",
        }
    ]
    pipeline = GatewayPipeline(policy_engine=policy)

    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="http",
            source_id="api",
            operation="read",
            identity=identity,
        )
    )

    assert decision.allowed is True
    assert decision.audit_metadata["legacy_role_match"] is True
    assert decision.audit_metadata["legacy_role_keys"] == ["legacy-reader"]


@pytest.mark.asyncio
async def test_pipeline_audit_emits_canonical_event() -> None:
    audit = MagicMock()
    audit.log = AsyncMock()
    pipeline = GatewayPipeline(audit_logger=audit)
    request = GatewayRequest(
        protocol="http",
        source_id="s1",
        operation="read",
        identity=_identity(),
        path="/v1/items",
    )

    await pipeline.audit(
        request,
        fingerprint="abc",
        cache_hit=True,
        cache_tier="l2",
        latency_ms=12.5,
        metadata={"status_code": 200},
    )

    record = audit.log.await_args.args[0]
    assert record.identity_id == 7
    assert record.source_id == "s1"
    assert record.operation == "read"
    assert record.cache_hit is True
    assert record.request_metadata["protocol"] == "http"
    assert record.request_metadata["status_code"] == 200


@pytest.mark.asyncio
async def test_pipeline_queues_high_risk_sql_write() -> None:
    classifier = MagicMock()
    classifier.classify.return_value = WriteClassification(
        risk_level=RiskLevel.HIGH,
        operation_type="DROP",
        tables=["users"],
        reason="DROP operations are high risk",
    )
    queue = MagicMock()
    queue.submit = AsyncMock(side_effect=WriteApprovalRequiredError(42, "high"))

    pipeline = GatewayPipeline(
        write_classifier=classifier,
        approval_queue=queue,
    )
    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="postgresql",
            source_id="s1",
            operation="write",
            identity=_identity(),
            sql="DROP TABLE users",
        )
    )

    assert decision.allowed is False
    assert decision.status_code == 202
    assert decision.approval_state == "pending"
    assert decision.approval_id == 42
    queue.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_multistatement_write_is_classified_before_read_execution() -> None:
    classifier = MagicMock()
    classifier.classify.return_value = WriteClassification(
        risk_level=RiskLevel.HIGH,
        operation_type="DROP",
        tables=["users"],
        reason="DROP operations are high risk",
    )
    queue = MagicMock()
    queue.submit = AsyncMock(side_effect=WriteApprovalRequiredError(4242, "high"))
    policy = MagicMock()
    policy.evaluate.return_value = PolicyDecision(allowed=True, reason="ok")

    pipeline = GatewayPipeline(
        write_classifier=classifier,
        approval_queue=queue,
        policy_engine=policy,
    )
    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="mcp",
            source_id="s1",
            operation="read",
            identity=_identity(),
            sql="SELECT 1; DROP TABLE users",
            tables=[],
        )
    )

    assert decision.allowed is False
    assert decision.status_code == 202
    assert decision.approval_id == 4242
    policy.evaluate.assert_called_once()
    assert policy.evaluate.call_args.kwargs["operation"] == "write"
    assert queue.submit.await_args.kwargs["metadata"]["normalized_operation"] == "DROP"


@pytest.mark.asyncio
async def test_allowed_low_risk_sql_write_returns_classification() -> None:
    classifier = MagicMock()
    classifier.classify.return_value = WriteClassification(
        risk_level=RiskLevel.LOW,
        operation_type="INSERT",
        tables=["logs"],
        reason="INSERT operation",
    )
    pipeline = GatewayPipeline(write_classifier=classifier)

    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="postgresql",
            source_id="s1",
            operation="read",
            identity=_identity(),
            sql="SELECT 1; INSERT INTO logs (msg) VALUES ('ok')",
        )
    )

    assert decision.allowed is True
    assert decision.write_classification is not None
    assert decision.write_classification.operation_type == "INSERT"
    assert decision.write_risk == "low"


@pytest.mark.asyncio
async def test_policy_write_risk_cap_blocks_before_approval_queue() -> None:
    classifier = MagicMock()
    classifier.classify.return_value = WriteClassification(
        risk_level=RiskLevel.HIGH,
        operation_type="DROP",
        tables=["users"],
        reason="DROP operations are high risk",
    )
    policy = MagicMock()
    policy.evaluate.return_value = PolicyDecision(
        allowed=True,
        reason="ok",
        write_risk_cap="medium",
    )
    queue = MagicMock()
    queue.submit = AsyncMock(side_effect=WriteApprovalRequiredError(55, "high"))

    pipeline = GatewayPipeline(
        write_classifier=classifier,
        policy_engine=policy,
        approval_queue=queue,
    )
    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="postgresql",
            source_id="s1",
            operation="write",
            identity=_identity(),
            sql="DROP TABLE users",
        )
    )

    assert decision.allowed is False
    assert decision.status_code == 403
    assert "Policy write risk cap exceeded" in decision.reason
    assert decision.audit_metadata["policy_write_risk_cap"] == "medium"
    queue.submit.assert_not_called()


@pytest.mark.asyncio
async def test_audit_canonical_columns_populated() -> None:
    audit = MagicMock()
    audit.log = AsyncMock()
    pipeline = GatewayPipeline(audit_logger=audit)
    request = GatewayRequest(
        protocol="http",
        source_id="api",
        operation="write",
        identity=_identity(),
        method="DELETE",
        path="/customers/1",
        metadata={"upstream_url": "https://upstream.example/customers/1"},
    )
    decision = MagicMock()
    decision.identity = _identity()
    decision.write_classification = WriteClassification(
        risk_level=RiskLevel.HIGH,
        operation_type="DELETE",
        reason="HTTP DELETE is destructive",
    )
    decision.audit_metadata = {
        "policy_decision": {"allowed": True, "reason": "ok"},
        "approval_id": 99,
        "approval_state": "pending",
    }
    decision.approval_id = 99
    decision.approval_state = "pending"
    decision.write_risk = "high"

    await pipeline.audit(request, decision=decision, status="queued")

    record = audit.log.await_args.args[0]
    assert record.protocol == "http"
    assert record.route == "/customers/1"
    assert record.normalized_operation == "DELETE"
    assert record.upstream_target == "https://upstream.example/customers/1"
    assert record.policy_decision == {"allowed": True, "reason": "ok"}
    assert record.approval_id == 99
    assert record.approval_status == "pending"
    assert record.risk_level == "high"


@pytest.mark.asyncio
async def test_pipeline_blocks_write_without_identity() -> None:
    pipeline = GatewayPipeline(approval_queue=MagicMock())

    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="http",
            source_id="api1",
            operation="write",
            method="DELETE",
            path="/records/1",
        )
    )

    assert decision.allowed is False
    assert decision.status_code == 403
    assert "identity/source unresolved" in decision.reason


@pytest.mark.asyncio
async def test_pipeline_queues_http_delete() -> None:
    queue = MagicMock()
    queue.submit = AsyncMock(side_effect=WriteApprovalRequiredError(99, "high"))
    pipeline = GatewayPipeline(approval_queue=queue)

    decision = await pipeline.preflight(
        GatewayRequest(
            protocol="http",
            source_id="api1",
            operation="write",
            identity=_identity(),
            method="DELETE",
            path="/records/1",
        )
    )

    assert decision.status_code == 202
    assert decision.approval_id == 99
    assert "DELETE" in queue.submit.await_args.kwargs["sql_text"]
