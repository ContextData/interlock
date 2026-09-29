"""End-to-end governance integration tests.

Verifies the full governance pipeline (auth -> policy -> write safety -> PII -> redaction)
using mocked PG pools and Redis clients. The actual classes under test are NOT mocked -
only their infrastructure dependencies (asyncpg.Pool, redis.asyncio.Redis).
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.config import AuthConfig, PIIConfig
from interlock.core.approval_queue import ApprovalQueue
from interlock.core.auth import AuthManager
from interlock.core.policy import PolicyEngine
from interlock.core.write_classifier import WriteClassifier
from interlock.errors import (
    IdentityNotFoundError,
    WriteApprovalRequiredError,
)
from interlock.models import (
    AgentType,
    IdentityContext,
    RiskLevel,
)
from interlock.pipeline.pii_fast import PIIFastScanner
from interlock.pipeline.processor import ResponseProcessor

# ---------------------------------------------------------------------------
# Helpers: mock PG pool that can return configurable rows
# ---------------------------------------------------------------------------


class _AsyncCtx:
    """Minimal async context manager for mocking pool.acquire()."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


def _make_mock_pg_pool(
    fetchrow_return=None,
    fetch_return=None,
    fetchval_return=None,
    execute_return="UPDATE 1",
):
    """Build a mock asyncpg.Pool with configurable return values."""
    pool = AsyncMock()

    # For pool.acquire() used as async context manager
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])

    # acquire() must return the context manager directly, not a coroutine.
    # Using MagicMock so calling it doesn't wrap in a coroutine.
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))

    # Direct pool methods used by some classes
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    pool.execute = AsyncMock(return_value=execute_return)

    return pool


def _make_mock_redis():
    """Build a mock redis.asyncio.Redis."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    return redis


def _identity_row(
    identity_id=1,
    name="test-agent",
    agent_type="claude_code",
    team="eng",
    roles=None,
    mapped_pg_role="readonly",
):
    """Build a dict that mimics an asyncpg Record for the identities table."""
    row = {
        "id": identity_id,
        "name": name,
        "agent_type": agent_type,
        "team": team,
        "roles": roles or ["analyst"],
        "mapped_pg_role": mapped_pg_role,
    }
    return row


def _policy_row(
    rule_id=1,
    name="allow-read",
    priority=10,
    conditions=None,
    actions=None,
    enabled=True,
):
    """Build a dict mimicking a policy_rules asyncpg Record."""
    return {
        "id": rule_id,
        "name": name,
        "priority": priority,
        "conditions": conditions or {},
        "actions": actions or {"effect": "allow"},
        "enabled": enabled,
    }


# ===================================================================
# Auth + Policy Flow
# ===================================================================


class TestAuthPolicyFlow:
    """Auth -> Policy integration tests."""

    @pytest.mark.asyncio
    async def test_authenticated_user_allowed_by_policy(self):
        """Authenticate user, policy allows read on specific source."""
        api_key = "onx-test-key-12345"
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        row = _identity_row(roles=["analyst"])
        pool = _make_mock_pg_pool(fetchrow_return=row)
        redis = _make_mock_redis()
        config = AuthConfig(session_ttl_seconds=3600)

        auth = AuthManager(pg_pool=pool, redis_client=redis, config=config)
        ctx = await auth.authenticate(api_key)

        assert ctx.user == "test-agent"
        assert ctx.identity_id == 1
        assert "analyst" in ctx.roles
        assert ctx.session_id is not None

        # Now evaluate policy - allow reads on source "pg-main" for role "analyst"
        policy_rules = [
            _policy_row(
                rule_id=1,
                name="analyst-read-pg-main",
                priority=10,
                conditions={
                    "source_ids": ["pg-main"],
                    "identity_roles": ["analyst"],
                    "operation_types": ["SELECT"],
                },
                actions={"effect": "allow"},
            ),
        ]
        policy_pool = _make_mock_pg_pool(fetch_return=policy_rules)
        engine = PolicyEngine(pg_pool=policy_pool)
        await engine.load()

        decision = engine.evaluate(
            identity=ctx,
            source_id="pg-main",
            operation="SELECT",
        )
        assert decision.allowed is True
        assert decision.rule_name == "analyst-read-pg-main"

    @pytest.mark.asyncio
    async def test_authenticated_user_denied_by_policy(self):
        """Authenticate user, policy denies write on specific source."""
        api_key = "onx-test-key-writer"
        row = _identity_row(roles=["analyst"])
        pool = _make_mock_pg_pool(fetchrow_return=row)
        redis = _make_mock_redis()
        config = AuthConfig()

        auth = AuthManager(pg_pool=pool, redis_client=redis, config=config)
        ctx = await auth.authenticate(api_key)

        # Rule: deny writes for analysts on pg-main
        policy_rules = [
            _policy_row(
                rule_id=2,
                name="deny-analyst-write",
                priority=20,
                conditions={
                    "source_ids": ["pg-main"],
                    "identity_roles": ["analyst"],
                    "operation_types": ["INSERT", "UPDATE", "DELETE"],
                },
                actions={"effect": "deny"},
            ),
        ]
        policy_pool = _make_mock_pg_pool(fetch_return=policy_rules)
        engine = PolicyEngine(pg_pool=policy_pool)
        await engine.load()

        decision = engine.evaluate(
            identity=ctx,
            source_id="pg-main",
            operation="INSERT",
        )
        assert decision.allowed is False
        assert decision.rule_name == "deny-analyst-write"

    @pytest.mark.asyncio
    async def test_unauthenticated_request_fails(self):
        """No matching API key raises IdentityNotFoundError."""
        pool = _make_mock_pg_pool(fetchrow_return=None)
        redis = _make_mock_redis()
        config = AuthConfig()

        auth = AuthManager(pg_pool=pool, redis_client=redis, config=config)

        with pytest.raises(IdentityNotFoundError):
            await auth.authenticate("bad-key-does-not-exist")

    @pytest.mark.asyncio
    async def test_policy_default_deny(self):
        """No matching rules results in default deny."""
        # Engine with no rules loaded
        policy_pool = _make_mock_pg_pool(fetch_return=[])
        engine = PolicyEngine(pg_pool=policy_pool)
        await engine.load()

        ctx = IdentityContext(
            identity_id=1,
            user="test-agent",
            agent_type=AgentType.CLAUDE_CODE,
            roles=["analyst"],
        )

        decision = engine.evaluate(
            identity=ctx,
            source_id="pg-main",
            operation="SELECT",
        )
        assert decision.allowed is False
        assert "default deny" in decision.reason.lower()


# ===================================================================
# Write Safety Flow
# ===================================================================


class TestWriteSafetyFlow:
    """Write classifier + approval queue integration tests."""

    def test_low_risk_write_auto_approved(self):
        """INSERT classified as low risk - proceeds without queuing."""
        classifier = WriteClassifier()
        result = classifier.classify("INSERT INTO logs (msg) VALUES ('hello')")

        assert result is not None
        assert result.risk_level == RiskLevel.LOW
        assert result.operation_type == "INSERT"
        assert result.blocked is False
        assert "logs" in result.tables

    def test_high_risk_write_queued(self):
        """DROP TABLE classified as high risk."""
        classifier = WriteClassifier()
        result = classifier.classify("DROP TABLE users")

        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DROP"
        assert result.blocked is False
        assert "users" in result.tables

    def test_blocked_execute_rejected(self):
        """EXECUTE statement is blocked entirely."""
        classifier = WriteClassifier()
        result = classifier.classify("EXECUTE prepared_stmt")

        assert result is not None
        assert result.blocked is True
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "EXECUTE"

    @pytest.mark.asyncio
    async def test_approval_lifecycle(self):
        """Submit -> approve -> execute flow through ApprovalQueue."""
        approval_id = 42
        pool = _make_mock_pg_pool(
            fetchval_return=approval_id,
            execute_return="UPDATE 1",
        )
        # After approve, get() is called to retrieve the record for execution
        pool.fetchrow = AsyncMock(
            return_value={
                "id": approval_id,
                "identity_id": 1,
                "source_id": "pg-main",
                "sql_text": "DELETE FROM old_data WHERE created_at < '2020-01-01'",
                "risk_level": "medium",
                "status": "approved",
                "approved_by": "admin",
                "executed_at": None,
                "expires_at": "2026-04-03T12:00:00",
                "request_metadata": "{}",
                "created_at": "2026-04-03T11:00:00",
            }
        )

        # Create a mock ConnectionManager that returns a mock pool for execution
        mock_conn_mgr = AsyncMock()
        exec_pool = AsyncMock()
        exec_pool.execute = AsyncMock(return_value="DELETE 5")
        mock_conn_mgr.get_pool = AsyncMock(return_value=exec_pool)

        queue = ApprovalQueue(
            pg_pool=pool,
            connection_manager=mock_conn_mgr,
            expiry_seconds=900,
        )

        # Step 1: Submit - raises WriteApprovalRequiredError
        with pytest.raises(WriteApprovalRequiredError) as exc_info:
            await queue.submit(
                identity_id=1,
                source_id="pg-main",
                sql_text="DELETE FROM old_data WHERE created_at < '2020-01-01'",
                risk_level=RiskLevel.MEDIUM,
            )
        assert exc_info.value.approval_id == approval_id
        assert exc_info.value.risk_level == "medium"

        # Step 2: Approve and execute
        result = await queue.approve(approval_id=approval_id, approved_by="admin")
        assert result["status"] == "approved"
        assert result["executed"] is True

        # Verify the actual SQL was executed on the target pool
        exec_pool.execute.assert_called_once_with(
            "DELETE FROM old_data WHERE created_at < '2020-01-01'"
        )

    def test_multi_statement_highest_risk(self):
        """'SELECT 1; DROP TABLE users;' yields HIGH risk from the DROP."""
        classifier = WriteClassifier()
        result = classifier.classify("SELECT 1; DROP TABLE users;")

        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DROP"


# ===================================================================
# PII Flow
# ===================================================================


class TestPIIFlow:
    """PII scanning and redaction tests."""

    def test_pii_detected_and_redacted(self):
        """Response with SSN gets redacted."""
        scanner = PIIFastScanner()
        text = "Customer SSN is 123-45-6789"
        matches = scanner.scan(text)

        assert len(matches) == 1
        assert matches[0].entity_type == "SSN"
        assert matches[0].text == "123-45-6789"

        redacted = scanner.redact(text, matches)
        assert "123-45-6789" not in redacted
        assert "[REDACTED:SSN]" in redacted

    def test_no_pii_passes_through(self):
        """Response without PII is unchanged."""
        scanner = PIIFastScanner()
        text = "Total sales for Q1: 42000 units"
        matches = scanner.scan(text)

        assert len(matches) == 0
        assert scanner.redact(text) == text

    def test_multiple_pii_types_redacted(self):
        """SSN + email + credit card all redacted."""
        scanner = PIIFastScanner()
        text = "SSN: 111-22-3333, " "email: alice@example.com, " "card: 4111-1111-1111-1111"
        matches = scanner.scan(text)

        entity_types = {m.entity_type for m in matches}
        assert "SSN" in entity_types
        assert "EMAIL" in entity_types
        assert "CREDIT_CARD" in entity_types

        redacted = scanner.redact(text, matches)
        assert "111-22-3333" not in redacted
        assert "alice@example.com" not in redacted
        assert "4111-1111-1111-1111" not in redacted
        assert "[REDACTED:SSN]" in redacted
        assert "[REDACTED:EMAIL]" in redacted
        assert "[REDACTED:CREDIT_CARD]" in redacted

    @pytest.mark.asyncio
    async def test_pii_in_free_text_field(self):
        """Field named 'description' with PII gets deep scan treatment via ResponseProcessor.

        Since we do not mock the deep scanner (optional dependency), we verify that
        the fast scanner still catches regex-detectable PII in free text fields.
        """
        scanner = PIIFastScanner()
        processor = ResponseProcessor(
            fast_scanner=scanner,
            deep_scanner=None,
            config=PIIConfig(fast_enabled=True, deep_enabled=False),
        )

        row = {
            "id": 1,
            "description": "Contact customer at 555-123-4567 regarding SSN 999-88-7777",
            "status": "open",
        }
        redacted_row, all_matches = await processor.process_row(row)

        # PII detected in description field
        assert len(all_matches) > 0
        entity_types = {m.entity_type for m in all_matches}
        assert "SSN" in entity_types
        assert "PHONE" in entity_types

        # description field redacted, other fields unchanged
        assert "999-88-7777" not in redacted_row["description"]
        assert "[REDACTED:SSN]" in redacted_row["description"]
        assert redacted_row["id"] == 1
        assert redacted_row["status"] == "open"


# ===================================================================
# Combined Flows
# ===================================================================


class TestCombinedFlows:
    """Full pipeline integration tests combining multiple components."""

    @pytest.mark.asyncio
    async def test_full_read_pipeline_with_pii_redaction(self):
        """Auth -> policy check -> read -> PII scan -> redact -> audit trail.

        Simulates a complete read request where the response data contains PII
        that must be detected and redacted before returning to the caller.
        """
        # --- Step 1: Authenticate ---
        api_key = "onx-full-pipeline-key"
        row = _identity_row(identity_id=10, name="data-agent", roles=["analyst"])
        auth_pool = _make_mock_pg_pool(fetchrow_return=row)
        redis = _make_mock_redis()
        config = AuthConfig()

        auth = AuthManager(pg_pool=auth_pool, redis_client=redis, config=config)
        identity = await auth.authenticate(api_key)

        assert identity.user == "data-agent"
        assert identity.identity_id == 10

        # --- Step 2: Policy check - allow read ---
        policy_rules = [
            _policy_row(
                rule_id=100,
                name="analyst-read-all",
                priority=50,
                conditions={
                    "identity_roles": ["analyst"],
                    "operation_types": ["SELECT"],
                },
                actions={"effect": "allow"},
            ),
        ]
        policy_pool = _make_mock_pg_pool(fetch_return=policy_rules)
        engine = PolicyEngine(pg_pool=policy_pool)
        await engine.load()

        decision = engine.evaluate(
            identity=identity,
            source_id="pg-main",
            operation="SELECT",
        )
        assert decision.allowed is True

        # --- Step 3: Simulate read result with PII ---
        query_results = [
            {
                "customer_id": 1,
                "name": "Alice Smith",
                "ssn": "123-45-6789",
                "email": "alice@example.com",
                "balance": "5000.00",
            },
            {
                "customer_id": 2,
                "name": "Bob Jones",
                "ssn": "987-65-4321",
                "email": "bob@example.com",
                "balance": "3200.00",
            },
        ]

        # --- Step 4: PII scan and redact ---
        scanner = PIIFastScanner()
        processor = ResponseProcessor(
            fast_scanner=scanner,
            deep_scanner=None,
            config=PIIConfig(fast_enabled=True, deep_enabled=False),
        )

        redacted_rows, pii_detected = await processor.process_rows(query_results)

        assert pii_detected is True
        assert len(redacted_rows) == 2

        # Verify SSNs are redacted
        for row in redacted_rows:
            assert "123-45-6789" not in str(row)
            assert "987-65-4321" not in str(row)
            assert "[REDACTED:SSN]" in row["ssn"]
            assert "[REDACTED:EMAIL]" in row["email"]

        # Non-PII fields unchanged
        assert redacted_rows[0]["customer_id"] == 1
        assert redacted_rows[0]["name"] == "Alice Smith"
        assert redacted_rows[0]["balance"] == "5000.00"

        # --- Step 5: Build audit record ---
        from interlock.models import AuditRecord

        all_pii_types: set[str] = set()
        for row in query_results:
            for field, value in row.items():
                if isinstance(value, str):
                    for m in scanner.scan(value):
                        all_pii_types.add(m.entity_type)

        audit = AuditRecord(
            identity_id=identity.identity_id,
            source_id="pg-main",
            operation="read",
            cache_hit=False,
            pii_detected=pii_detected,
            pii_types=sorted(all_pii_types),
            status="success",
        )
        assert audit.pii_detected is True
        assert "SSN" in audit.pii_types
        assert "EMAIL" in audit.pii_types

    @pytest.mark.asyncio
    async def test_full_write_pipeline_with_approval(self):
        """Auth -> policy check -> classify -> queue -> approve.

        Simulates a complete write request where the SQL is classified as high risk,
        queued for approval, then approved and executed.
        """
        # --- Step 1: Authenticate ---
        api_key = "onx-writer-key"
        row = _identity_row(identity_id=20, name="deploy-bot", roles=["deployer"])
        auth_pool = _make_mock_pg_pool(fetchrow_return=row)
        redis = _make_mock_redis()
        config = AuthConfig()

        auth = AuthManager(pg_pool=auth_pool, redis_client=redis, config=config)
        identity = await auth.authenticate(api_key)

        assert identity.user == "deploy-bot"

        # --- Step 2: Policy check - allow writes for deployer ---
        policy_rules = [
            _policy_row(
                rule_id=200,
                name="deployer-write-all",
                priority=50,
                conditions={
                    "identity_roles": ["deployer"],
                    "operation_types": ["INSERT", "UPDATE", "DELETE", "DROP"],
                },
                actions={"effect": "allow"},
            ),
        ]
        policy_pool = _make_mock_pg_pool(fetch_return=policy_rules)
        engine = PolicyEngine(pg_pool=policy_pool)
        await engine.load()

        sql = "DROP TABLE temp_staging"
        decision = engine.evaluate(
            identity=identity,
            source_id="pg-main",
            operation="DROP",
            tables=["temp_staging"],
        )
        assert decision.allowed is True

        # --- Step 3: Classify the write ---
        classifier = WriteClassifier(critical_tables={"users", "accounts"})
        classification = classifier.classify(sql)

        assert classification is not None
        assert classification.risk_level == RiskLevel.HIGH
        assert classification.operation_type == "DROP"
        assert classification.blocked is False

        # --- Step 4: Queue for approval ---
        approval_id = 99
        queue_pool = _make_mock_pg_pool(
            fetchval_return=approval_id,
            execute_return="UPDATE 1",
        )
        queue_pool.fetchrow = AsyncMock(
            return_value={
                "id": approval_id,
                "identity_id": identity.identity_id,
                "source_id": "pg-main",
                "sql_text": sql,
                "risk_level": "high",
                "status": "approved",
                "approved_by": "admin",
                "executed_at": None,
                "expires_at": "2026-04-03T12:00:00",
                "request_metadata": "{}",
                "created_at": "2026-04-03T11:00:00",
            }
        )

        mock_conn_mgr = AsyncMock()
        exec_pool = AsyncMock()
        exec_pool.execute = AsyncMock(return_value="DROP TABLE")
        mock_conn_mgr.get_pool = AsyncMock(return_value=exec_pool)

        queue = ApprovalQueue(
            pg_pool=queue_pool,
            connection_manager=mock_conn_mgr,
            expiry_seconds=900,
        )

        # Submit raises WriteApprovalRequiredError
        with pytest.raises(WriteApprovalRequiredError) as exc_info:
            await queue.submit(
                identity_id=identity.identity_id,
                source_id="pg-main",
                sql_text=sql,
                risk_level=classification.risk_level,
            )
        assert exc_info.value.approval_id == approval_id
        assert exc_info.value.risk_level == "high"

        # --- Step 5: Approve and execute ---
        result = await queue.approve(approval_id=approval_id, approved_by="admin")
        assert result["status"] == "approved"
        assert result["executed"] is True

        # Verify the DROP was executed on the target data source
        exec_pool.execute.assert_called_once_with(sql)
