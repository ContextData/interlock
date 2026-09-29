"""SQL name resolution and the shared governance derivation.

The resolution rules decide which table a role or policy is evaluated against,
so each dialect's rule is pinned here, along with the two compatibility
guarantees: legacy `public.` roles keep working on MySQL and Snowflake, and a
deny can never be routed around by a stale catalog.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.core.policy import PolicyEngine
from interlock.core.source_roles import (
    PermissionStatement,
    SourceRoleEvaluator,
    build_permission_request,
)
from interlock.core.sql_governance import (
    derive_sql,
    legacy_naming,
    naming_for,
    resolve_table,
    table_spellings,
)
from interlock.models import AgentType, IdentityContext, PolicyRule

PG = naming_for(
    "postgresql",
    {"database": "app"},
    {"effective_schemas": ["tenant", "public"], "database": "app"},
    frozenset({("tenant", "orders"), ("public", "orders"), ("public", "customers")}),
)
MYSQL = naming_for("mysql", {"database": "Shop"})
SNOWFLAKE = naming_for("snowflake", {"database": "ANALYTICS", "schema": "SALES"})


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_postgres_resolves_to_the_first_schema_on_the_path_that_holds_the_table() -> None:
    orders = resolve_table("orders", PG)
    assert (orders.canonical, orders.resolved, orders.shadows) == ("tenant.orders", True, ())
    customers = resolve_table("customers", PG)
    assert customers.canonical == "public.customers"
    assert customers.resolved is True
    # tenant comes first on the path: a customers table created there since the
    # last scan would win, so a deny on it still has to fire.
    assert customers.shadows == ("tenant.customers",)


def test_postgres_names_missing_from_the_catalog_shadow_the_whole_path() -> None:
    fresh = resolve_table("invoices", PG)
    assert fresh.canonical == "tenant.invoices"
    assert fresh.resolved is False
    assert fresh.shadows == ("public.invoices",)


def test_postgres_system_relations_are_shadowed_by_pg_catalog() -> None:
    assert "pg_catalog.pg_authid" in resolve_table("pg_authid", PG).shadows


def test_qualified_names_are_taken_as_written_but_lower_cased() -> None:
    table = resolve_table('Tenant."Orders"', PG)
    assert (table.canonical, table.shadows, table.aliases) == ("tenant.orders", (), ())


def test_without_a_catalog_postgres_names_resolve_as_they_always_did() -> None:
    table = resolve_table("orders", naming_for("postgresql", {}))
    assert (table.canonical, table.resolved, table.shadows) == ("public.orders", False, ())


def test_mysql_resolves_to_the_configured_database_with_a_legacy_alias() -> None:
    table = resolve_table("Customers", MYSQL)
    assert table.canonical == "shop.customers"
    assert table.aliases == ("public.customers",)
    assert resolve_table("other.customers", MYSQL).aliases == ()


def test_snowflake_resolves_to_the_configured_schema() -> None:
    assert resolve_table("orders", SNOWFLAKE).canonical == "sales.orders"
    assert resolve_table("orders", SNOWFLAKE).aliases == ("public.orders",)
    same_db = resolve_table("analytics.sales.orders", SNOWFLAKE)
    assert (same_db.canonical, same_db.refusal) == ("sales.orders", None)


def test_snowflake_refuses_a_reference_into_another_database() -> None:
    other = resolve_table("otherdb.sales.orders", SNOWFLAKE)
    assert other.refusal and "otherdb.sales.orders" in other.refusal


def test_catalog_facts_override_configuration() -> None:
    naming = naming_for("mysql", {"database": "configured"}, {"default_schema": "actual"})
    assert resolve_table("t", naming).canonical == "actual.t"


# --------------------------------------------------------------------------
# Permission requests
# --------------------------------------------------------------------------


def _request(tables: list[str], naming: Any, **kwargs: Any) -> Any:
    return build_permission_request(
        protocol="mcp",
        source_id="s",
        identity_id=1,
        operation="SELECT",
        tables=tables,
        naming=naming,
        **kwargs,
    )


def test_requests_carry_canonical_resources_with_their_variants() -> None:
    request = _request(["customers"], MYSQL)
    assert request.resources == ["shop.customers.*"]
    assert request.resource_variants == {"shop.customers.*": {"aliases": ["public.customers.*"]}}
    assert request.naming_resolved is False


def test_a_request_without_naming_keeps_the_legacy_resource() -> None:
    request = _request(["customers"], None)
    assert request.resources == ["public.customers.*"]
    assert request.resource_variants == {}
    assert request.naming_resolved is None


def test_columns_belong_to_their_own_table() -> None:
    request = _request(
        ["customers", "orders"],
        PG,
        columns=["customers.email", "orders.total"],
    )
    assert request.resources == ["public.customers.email", "tenant.orders.total"]


def test_a_cross_database_reference_is_recorded_as_a_refusal() -> None:
    request = _request(["otherdb.sales.orders"], SNOWFLAKE)
    assert request.naming_refusals


# --------------------------------------------------------------------------
# Evaluation against variants
# --------------------------------------------------------------------------


def _statement(effect: str, pattern: str, permission_id: int = 1) -> PermissionStatement:
    return PermissionStatement(
        permission_id=permission_id,
        role_id=1,
        role_key="r",
        effect=effect,
        action="db.table.select",
        resource_type="db.table",
        resource_pattern=pattern,
        constraints={},
    )


async def _evaluate(request: Any, *statements: PermissionStatement) -> Any:
    evaluator = SourceRoleEvaluator(MagicMock())
    evaluator._load_statements = AsyncMock(return_value=list(statements))  # type: ignore[method-assign]
    return await evaluator.evaluate(request)


async def test_a_legacy_public_allow_still_covers_a_mysql_table() -> None:
    decision = await _evaluate(_request(["customers"], MYSQL), _statement("allow", "public.*"))
    assert decision.allowed


async def test_a_legacy_public_deny_still_fires_on_a_mysql_table() -> None:
    decision = await _evaluate(
        _request(["customers"], MYSQL),
        _statement("allow", "*"),
        _statement("deny", "public.customers.*", permission_id=2),
    )
    assert not decision.allowed and decision.explicit_deny


async def test_a_role_on_the_real_database_name_now_matches() -> None:
    decision = await _evaluate(_request(["customers"], MYSQL), _statement("allow", "shop.*"))
    assert decision.allowed


async def test_a_shadow_fires_a_deny_but_never_an_allow() -> None:
    shadowed = _request(["customers"], PG)
    denied = await _evaluate(
        shadowed,
        _statement("allow", "*"),
        _statement("deny", "tenant.customers.*", permission_id=2),
    )
    assert not denied.allowed and denied.explicit_deny
    # An allow on the shadow alone does not cover the table actually read.
    not_covered = await _evaluate(shadowed, _statement("allow", "tenant.*"))
    assert not not_covered.allowed


async def test_a_cross_database_reference_is_denied_whatever_the_roles_say() -> None:
    decision = await _evaluate(
        _request(["otherdb.sales.orders"], SNOWFLAKE), _statement("allow", "*")
    )
    assert not decision.allowed
    assert "otherdb.sales.orders" in decision.reason


# --------------------------------------------------------------------------
# Policy: deny rules match every spelling, allow rules only as written
# --------------------------------------------------------------------------


def _engine(*rules: PolicyRule) -> PolicyEngine:
    engine = PolicyEngine(MagicMock())
    engine._rules = sorted(rules, key=lambda r: r.priority, reverse=True)
    return engine


def _rule(rule_id: int, effect: str, tables: list[str], priority: int) -> PolicyRule:
    return PolicyRule(
        id=rule_id,
        name=f"{effect}-{rule_id}",
        priority=priority,
        conditions={"tables": tables},
        actions={"effect": effect},
    )


_IDENTITY = IdentityContext(identity_id=1, user="a", agent_type=AgentType.CUSTOM)


@pytest.mark.parametrize("written", ["payroll", "sales.payroll", "SALES.PAYROLL"])
def test_a_deny_policy_fires_whichever_spelling_the_query_uses(written: str) -> None:
    naming = naming_for("mysql", {"database": "sales"})
    engine = _engine(_rule(1, "deny", ["sales.payroll"], 90), _rule(2, "allow", [], 1))
    decision = engine.evaluate(
        _IDENTITY,
        "s",
        "read",
        tables=[written],
        deny_table_spellings=table_spellings([resolve_table(written, naming)]),
    )
    assert not decision.allowed, written


def test_an_allow_policy_is_not_broadened_past_a_lower_priority_deny() -> None:
    # Before this change the allow on bare `payroll` did not match a query on
    # `sales.payroll`, so the deny below it decided. It still must.
    engine = _engine(_rule(1, "allow", ["payroll"], 90), _rule(2, "deny", [], 10))
    decision = engine.evaluate(
        _IDENTITY,
        "s",
        "read",
        tables=["sales.payroll"],
        deny_table_spellings=table_spellings([resolve_table("sales.payroll", legacy_naming())]),
    )
    assert not decision.allowed
    assert decision.rule_id == 2


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def test_every_statement_contributes_its_tables() -> None:
    derivation = derive_sql("SELECT 1; SELECT * FROM payroll", source_id="s", dialect="postgres")
    assert derivation.tables == ["payroll"]


def test_a_three_part_name_keeps_its_database() -> None:
    derivation = derive_sql("SELECT * FROM otherdb.sch.t", source_id="s", dialect="snowflake")
    assert [t.lower() for t in derivation.tables] == ["otherdb.sch.t"]


# --------------------------------------------------------------------------
# The compatibility report
# --------------------------------------------------------------------------


def _findings(naming: Any, effect: str, pattern: str) -> list[Any]:
    from interlock.catalog.compat import findings_for_statement

    return findings_for_statement(
        source_id="s", naming=naming, role_key="r", effect=effect, pattern=pattern
    )


def test_a_mysql_public_statement_is_reported_as_a_legacy_alias() -> None:
    (finding,) = _findings(MYSQL, "allow", "public.customers.*")
    assert finding.kind == "legacy_alias"
    assert finding.suggestion == "shop.customers.*"


def test_a_postgres_public_allow_on_a_non_public_path_is_reported_as_narrowed() -> None:
    (allow,) = _findings(PG, "allow", "public.*")
    assert (allow.kind, allow.suggestion) == ("no_longer_covers_bare_names", "tenant.*")
    (deny,) = _findings(PG, "deny", "public.orders.*")
    assert deny.kind == "deny_now_scoped"


def test_statements_whose_meaning_does_not_change_are_not_reported() -> None:
    assert _findings(MYSQL, "allow", "*.*") == []
    assert _findings(MYSQL, "allow", "shop.customers.*") == []
    assert _findings(naming_for("postgresql", {}), "allow", "public.*") == []
