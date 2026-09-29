"""Tests for the RBAC policy engine."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.core.policy import PolicyEngine
from interlock.models import AgentType, IdentityContext, PolicyRule

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity(
    roles: list[str] | None = None,
    user: str = "test-user",
    team: str | None = None,
    source_roles: list[dict[str, str]] | None = None,
) -> IdentityContext:
    return IdentityContext(
        identity_id=1,
        user=user,
        agent_type=AgentType.CLAUDE_CODE,
        team=team,
        roles=roles or [],
        source_roles=source_roles or [],
    )


def _rule(
    rule_id: int = 1,
    name: str = "r1",
    priority: int = 0,
    conditions: dict | None = None,
    effect: str = "allow",
    redact_columns: list[str] | None = None,
    rate_limit: dict | None = None,
    actions: dict | None = None,
) -> PolicyRule:
    actions = dict(actions or {"effect": effect})
    if redact_columns:
        actions["redact_columns"] = redact_columns
    if rate_limit:
        actions["rate_limit"] = rate_limit
    return PolicyRule(
        id=rule_id,
        name=name,
        priority=priority,
        conditions=conditions or {},
        actions=actions,
    )


def _make_engine(rules: list[PolicyRule] | None = None) -> PolicyEngine:
    """Create a PolicyEngine with rules injected directly (no PG needed)."""
    pool = MagicMock()
    engine = PolicyEngine(pool)
    engine._rules = rules or []
    return engine


# ---------------------------------------------------------------------------
# Default deny
# ---------------------------------------------------------------------------


class TestDefaultDeny:
    def test_no_rules_denies(self):
        engine = _make_engine([])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is False
        assert decision.rule_id is None
        assert "default deny" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Allow / Deny effects
# ---------------------------------------------------------------------------


class TestAllowDeny:
    def test_allow_rule_matches(self):
        engine = _make_engine([_rule(effect="allow")])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is True
        assert decision.rule_id == 1

    def test_deny_rule_matches(self):
        engine = _make_engine([_rule(effect="deny")])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is False
        assert decision.rule_id == 1
        assert "deny" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    def test_higher_priority_wins(self):
        low = _rule(rule_id=1, name="low", priority=1, effect="deny")
        high = _rule(rule_id=2, name="high", priority=10, effect="allow")
        # Rules must be sorted by priority DESC (engine expects pre-sorted from load)
        engine = _make_engine([high, low])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is True
        assert decision.rule_id == 2

    def test_lower_priority_loses(self):
        high = _rule(rule_id=1, name="high-deny", priority=10, effect="deny")
        low = _rule(rule_id=2, name="low-allow", priority=1, effect="allow")
        engine = _make_engine([high, low])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is False
        assert decision.rule_id == 1


# ---------------------------------------------------------------------------
# Role matching
# ---------------------------------------------------------------------------


class TestRoleMatching:
    def test_identity_role_match_uses_identity_roles_condition(self):
        rule = _rule(conditions={"identity_roles": ["analyst", "admin"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(roles=["analyst"]), "src1", "read")
        assert decision.allowed is True

    def test_source_role_condition_does_not_match_global_identity_roles(self):
        rule = _rule(conditions={"roles": ["viewer"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(roles=["viewer"]), "src1", "read")
        assert decision.allowed is False
        assert decision.rule_id is None

    def test_identity_role_intersection(self):
        rule = _rule(conditions={"identity_roles": ["admin", "superuser"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(roles=["viewer", "superuser"]), "src1", "read")
        assert decision.allowed is True

    def test_source_scoped_role_match(self):
        rule = _rule(conditions={"source_ids": ["pg_reporting"], "roles": ["analyst"]})
        engine = _make_engine([rule])
        identity = _identity(source_roles=[{"source_id": "pg_reporting", "role": "analyst"}])

        decision = engine.evaluate(identity, "pg_reporting", "read")

        assert decision.allowed is True

    def test_source_scoped_role_does_not_bleed_to_other_source(self):
        rule = _rule(conditions={"source_ids": ["pg_billing"], "roles": ["analyst"]})
        engine = _make_engine([rule])
        identity = _identity(source_roles=[{"source_id": "pg_reporting", "role": "analyst"}])

        decision = engine.evaluate(identity, "pg_billing", "read")

        assert decision.allowed is False
        assert decision.rule_id is None

    def test_legacy_metadata_source_role_does_not_match_source_role_condition(self):
        rule = _rule(conditions={"source_ids": ["pg_reporting"], "roles": ["analyst"]})
        engine = _make_engine([rule])
        identity = _identity(
            source_roles=[
                {
                    "source_id": "pg_reporting",
                    "role": "analyst",
                    "authority": "legacy_metadata",
                }
            ]
        )

        decision = engine.evaluate(identity, "pg_reporting", "read")

        assert decision.allowed is False
        assert decision.rule_id is None

    def test_roles_and_identity_roles_are_distinct_conditions(self):
        rule = _rule(
            conditions={
                "source_ids": ["pg_reporting"],
                "roles": ["source_reader"],
                "identity_roles": ["platform"],
            }
        )
        engine = _make_engine([rule])

        source_only = engine.evaluate(
            _identity(
                roles=["other"],
                source_roles=[
                    {
                        "source_id": "pg_reporting",
                        "role": "source_reader",
                        "authority": "identity_source_role_grants",
                    }
                ],
            ),
            "pg_reporting",
            "read",
        )
        identity_only = engine.evaluate(
            _identity(roles=["platform"]),
            "pg_reporting",
            "read",
        )
        both = engine.evaluate(
            _identity(
                roles=["platform"],
                source_roles=[
                    {
                        "source_id": "pg_reporting",
                        "role": "source_reader",
                        "authority": "identity_source_role_grants",
                    }
                ],
            ),
            "pg_reporting",
            "read",
        )

        assert source_only.allowed is False
        assert identity_only.allowed is False
        assert both.allowed is True


# ---------------------------------------------------------------------------
# Source ID matching
# ---------------------------------------------------------------------------


class TestSourceIdMatching:
    def test_source_match(self):
        rule = _rule(conditions={"source_ids": ["pg_main", "pg_replica"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "pg_main", "read")
        assert decision.allowed is True

    def test_source_no_match(self):
        rule = _rule(conditions={"source_ids": ["pg_main"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "pg_other", "read")
        assert decision.allowed is False
        assert decision.rule_id is None


# ---------------------------------------------------------------------------
# Operation type matching
# ---------------------------------------------------------------------------


class TestOperationTypeMatching:
    def test_operation_match(self):
        rule = _rule(conditions={"operation_types": ["read", "discover"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is True

    def test_operation_no_match(self):
        rule = _rule(conditions={"operation_types": ["write"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# Table matching
# ---------------------------------------------------------------------------


class TestTableMatching:
    def test_table_match(self):
        rule = _rule(conditions={"tables": ["users", "orders"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read", tables=["orders"])
        assert decision.allowed is True

    def test_table_no_match(self):
        rule = _rule(conditions={"tables": ["users"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read", tables=["secrets"])
        assert decision.allowed is False

    def test_table_condition_with_no_request_tables(self):
        rule = _rule(conditions={"tables": ["users"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read", tables=None)
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# Column matching
# ---------------------------------------------------------------------------


class TestColumnMatching:
    def test_column_match(self):
        rule = _rule(conditions={"columns": ["email", "ssn"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read", columns=["email"])
        assert decision.allowed is True

    def test_column_no_match(self):
        rule = _rule(conditions={"columns": ["ssn"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read", columns=["name"])
        assert decision.allowed is False

    def test_column_condition_with_no_request_columns(self):
        rule = _rule(conditions={"columns": ["ssn"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read", columns=None)
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# Classification matching
# ---------------------------------------------------------------------------


class TestClassificationMatching:
    def test_classification_tags_match_when_request_has_any_tag(self):
        rule = _rule(conditions={"classification_tags": ["pii", "restricted"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(
            _identity(),
            "src1",
            "read",
            classification_tags=["pii"],
        )
        assert decision.allowed is True
        assert decision.rule_id == 1

    def test_classification_tags_do_not_match_when_missing(self):
        rule = _rule(conditions={"classification_tags": ["restricted"]})
        engine = _make_engine([rule])
        decision = engine.evaluate(
            _identity(),
            "src1",
            "read",
            classification_tags=["public"],
        )
        assert decision.allowed is False
        assert decision.rule_id is None


# ---------------------------------------------------------------------------
# Wildcard conditions (empty = any)
# ---------------------------------------------------------------------------


class TestWildcardConditions:
    def test_empty_conditions_match_everything(self):
        rule = _rule(conditions={})
        engine = _make_engine([rule])
        decision = engine.evaluate(
            _identity(roles=["any"]),
            "any_source",
            "write",
            tables=["any_table"],
            columns=["any_col"],
        )
        assert decision.allowed is True

    def test_empty_list_matches_everything(self):
        rule = _rule(
            conditions={
                "source_ids": [],
                "roles": [],
                "operation_types": [],
                "tables": [],
                "columns": [],
            }
        )
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is True

    def test_none_values_match_everything(self):
        rule = _rule(
            conditions={
                "source_ids": None,
                "roles": None,
                "operation_types": None,
                "tables": None,
                "columns": None,
            }
        )
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.allowed is True

    def test_partial_wildcard(self):
        """Some conditions specified, others missing (wildcard)."""
        rule = _rule(conditions={"roles": ["admin"]})
        engine = _make_engine([rule])
        # source_id, operation, tables, columns are all wildcard
        decision = engine.evaluate(
            _identity(source_roles=[{"source_id": "any_src", "role": "admin"}]),
            "any_src",
            "write",
            tables=["any"],
            columns=["any"],
        )
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Multiple rules, first match wins
# ---------------------------------------------------------------------------


class TestMultipleRules:
    def test_first_match_wins(self):
        # Both rules match, but first (higher priority) should win
        deny_high = _rule(
            rule_id=1,
            name="deny-all",
            priority=100,
            conditions={"identity_roles": ["viewer"]},
            effect="deny",
        )
        allow_low = _rule(
            rule_id=2,
            name="allow-all",
            priority=1,
            conditions={},
            effect="allow",
        )
        engine = _make_engine([deny_high, allow_low])
        decision = engine.evaluate(_identity(roles=["viewer"]), "src1", "read")
        assert decision.allowed is False
        assert decision.rule_id == 1

    def test_skip_non_matching_rules(self):
        no_match = _rule(
            rule_id=1,
            name="admin-only",
            priority=100,
            conditions={"identity_roles": ["admin"]},
            effect="deny",
        )
        match = _rule(
            rule_id=2,
            name="viewer-allow",
            priority=50,
            conditions={"identity_roles": ["viewer"]},
            effect="allow",
        )
        engine = _make_engine([no_match, match])
        decision = engine.evaluate(_identity(roles=["viewer"]), "src1", "read")
        assert decision.allowed is True
        assert decision.rule_id == 2

    def test_combined_conditions(self):
        """Rule with multiple conditions - all must match."""
        rule = _rule(
            conditions={
                "source_ids": ["pg_main"],
                "roles": ["analyst"],
                "operation_types": ["read"],
                "tables": ["reports"],
            }
        )
        engine = _make_engine([rule])

        # All conditions met
        decision = engine.evaluate(
            _identity(source_roles=[{"source_id": "pg_main", "role": "analyst"}]),
            "pg_main",
            "read",
            tables=["reports"],
        )
        assert decision.allowed is True

        # Source mismatch
        decision = engine.evaluate(
            _identity(source_roles=[{"source_id": "pg_main", "role": "analyst"}]),
            "pg_other",
            "read",
            tables=["reports"],
        )
        assert decision.allowed is False

        # Role mismatch
        decision = engine.evaluate(
            _identity(source_roles=[{"source_id": "pg_main", "role": "viewer"}]),
            "pg_main",
            "read",
            tables=["reports"],
        )
        assert decision.allowed is False

        # Operation mismatch
        decision = engine.evaluate(
            _identity(source_roles=[{"source_id": "pg_main", "role": "analyst"}]),
            "pg_main",
            "write",
            tables=["reports"],
        )
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# Redact columns and rate_limit in decision
# ---------------------------------------------------------------------------


class TestActionFields:
    def test_redact_columns_propagated(self):
        rule = _rule(redact_columns=["ssn", "credit_card"])
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.redact_columns == ["ssn", "credit_card"]

    def test_rate_limit_propagated(self):
        rl = {"requests_per_minute": 60}
        rule = _rule(rate_limit=rl)
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "read")
        assert decision.rate_limit == {"limit": 60, "window_seconds": 60}

    def test_write_risk_cap_propagated(self):
        rule = _rule(actions={"effect": "allow", "max_write_risk": "medium"})
        engine = _make_engine([rule])
        decision = engine.evaluate(_identity(), "src1", "write")
        assert decision.write_risk_cap == "medium"


# ---------------------------------------------------------------------------
# Load from PG (mocked pool)
# ---------------------------------------------------------------------------


class TestLoadFromPG:
    @pytest.mark.asyncio
    async def test_load_populates_rules(self):
        rows = [
            {
                "id": 1,
                "name": "allow-all",
                "priority": 10,
                "conditions": {"roles": ["admin"]},
                "actions": {"effect": "allow"},
                "enabled": True,
            },
            {
                "id": 2,
                "name": "deny-writes",
                "priority": 5,
                "conditions": {"operation_types": ["write"]},
                "actions": {"effect": "deny"},
                "enabled": True,
            },
        ]
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=rows)

        engine = PolicyEngine(pool)
        await engine.load()

        assert len(engine._rules) == 2
        assert engine._rules[0].name == "allow-all"
        assert engine._rules[0].priority == 10
        assert engine._rules[1].name == "deny-writes"

    @pytest.mark.asyncio
    async def test_load_handles_json_string_conditions(self):
        """Conditions/actions stored as JSON strings should be parsed."""
        rows = [
            {
                "id": 1,
                "name": "r1",
                "priority": 0,
                "conditions": json.dumps({"roles": ["admin"]}),
                "actions": json.dumps({"effect": "allow"}),
                "enabled": True,
            },
        ]
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=rows)

        engine = PolicyEngine(pool)
        await engine.load()

        assert engine._rules[0].conditions == {"roles": ["admin"]}
        assert engine._rules[0].actions == {"effect": "allow"}


# ---------------------------------------------------------------------------
# NOTIFY triggers reload
# ---------------------------------------------------------------------------


class TestNotifyReload:
    @pytest.mark.asyncio
    async def test_setup_listener_subscribes(self):
        mock_conn = AsyncMock()
        pool = AsyncMock()
        pool.acquire = AsyncMock(return_value=mock_conn)

        engine = PolicyEngine(pool)
        await engine.setup_listener()

        mock_conn.add_listener.assert_called_once_with("onyx_policy_changed", engine._on_notify)

    @pytest.mark.asyncio
    async def test_on_notify_triggers_reload(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[])

        engine = PolicyEngine(pool)
        # Preload with a rule
        engine._rules = [_rule()]
        assert len(engine._rules) == 1

        # Simulate notification callback
        engine._on_notify(MagicMock(), 0, "onyx_policy_changed", "")

        # Let the scheduled coroutine run
        await asyncio.sleep(0.05)

        # Should have called fetch (reload) and cleared rules since fetch returns []
        pool.fetch.assert_called_once()
        assert len(engine._rules) == 0

    @pytest.mark.asyncio
    async def test_notify_during_reload_runs_second_reload_pass(self):
        pool = AsyncMock()
        engine = PolicyEngine(pool)
        calls = 0

        async def slow_load() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                engine._on_notify(MagicMock(), 0, "onyx_policy_changed", "")
                await asyncio.sleep(0)

        engine.load = AsyncMock(side_effect=slow_load)  # type: ignore[method-assign]

        engine._on_notify(MagicMock(), 0, "onyx_policy_changed", "")
        assert engine._reload_task is not None
        await asyncio.wait_for(engine._reload_task, timeout=1)

        assert calls == 2
        assert engine._applied_reload_generation == engine._reload_generation == 2
