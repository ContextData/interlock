"""Which columns a statement touches, per table - the input column rules need."""

from __future__ import annotations

import pytest

from interlock.core.sql_columns import ColumnAccess, column_access

CATALOG = {
    ("public", "customers"): ("id", "name", "email", "ssn"),
    ("public", "orders"): ("id", "customer_id", "total"),
}


def _resolve(written: str) -> str:
    return written.lower() if "." in written else f"public.{written.lower()}"


def _access(sql: str, catalog: object = CATALOG) -> ColumnAccess:
    return column_access(sql, dialect="postgres", resolve=_resolve, catalog=catalog)  # type: ignore[arg-type]


def _columns(access: ColumnAccess) -> dict[str, list[str]]:
    return {table: sorted(cols) for table, cols in access.columns.items()}


def test_select_star_is_expanded_from_the_catalog() -> None:
    access = _access("SELECT * FROM customers")
    assert _columns(access) == {"public.customers": ["email", "id", "name", "ssn"]}
    assert access.gaps == {}


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT c.name, o.total FROM customers c JOIN orders o ON o.customer_id = c.id "
            "WHERE c.ssn = '1'",
            {"public.customers": ["id", "name", "ssn"], "public.orders": ["customer_id", "total"]},
        ),
        (
            "SELECT name FROM customers GROUP BY name ORDER BY max(email)",
            {"public.customers": ["email", "name"]},
        ),
        (
            "SELECT ssn AS x FROM (SELECT ssn FROM customers) s",
            {"public.customers": ["ssn"]},
        ),
        (
            "WITH r AS (SELECT ssn FROM customers) SELECT ssn FROM r",
            {"public.customers": ["ssn"]},
        ),
    ],
    ids=["join-and-where", "group-and-order", "subquery", "cte"],
)
def test_every_clause_counts_as_access(sql: str, expected: dict[str, list[str]]) -> None:
    access = _access(sql)
    assert _columns(access) == expected
    assert access.gaps == {}


def test_count_star_touches_the_table_without_naming_a_column() -> None:
    assert _columns(_access("SELECT count(*) FROM customers")) == {"public.customers": []}


@pytest.mark.parametrize(
    "sql",
    ["SELECT to_jsonb(c) FROM customers c", "SELECT row_to_json(c.*) FROM customers c"],
)
def test_a_whole_row_reference_is_a_gap_not_an_empty_read(sql: str) -> None:
    assert "public.customers" in _access(sql).gaps


def test_an_unknown_column_is_a_gap() -> None:
    assert "could not be resolved" in _access("SELECT nope FROM customers").gaps["public.customers"]


def test_a_table_missing_from_the_catalog_is_a_gap() -> None:
    assert "public.invoices" in _access("SELECT name FROM invoices").gaps


def test_without_a_catalog_every_table_is_a_gap() -> None:
    assert set(_access("SELECT name FROM customers", catalog=None).gaps) == {"public.customers"}


def test_writes_record_the_columns_they_set_and_filter_on() -> None:
    assert _columns(_access("UPDATE customers SET email = 'a' WHERE id = 1")) == {
        "public.customers": ["email", "id"]
    }
    assert _columns(_access("INSERT INTO customers (id, name) VALUES (1, 'x')")) == {
        "public.customers": ["id", "name"]
    }
    # No column list writes every column.
    assert _columns(_access("INSERT INTO customers VALUES (1, 'x', 'e', 's')")) == {
        "public.customers": ["email", "id", "name", "ssn"]
    }


def test_a_delete_needs_the_whole_table() -> None:
    access = _access("DELETE FROM customers WHERE ssn = '1'")
    assert access.whole_table == {"public.customers"}
    assert _columns(access) == {"public.customers": ["ssn"]}


def test_every_statement_in_a_message_counts() -> None:
    assert _columns(_access("SELECT 1; SELECT email FROM customers")) == {
        "public.customers": ["email"]
    }


def test_lineage_follows_an_alias_back_to_its_base_column() -> None:
    access = _access("SELECT ssn AS x, upper(name) AS n FROM (SELECT ssn, name FROM customers) s")
    assert access.lineage == {
        "x": {"public.customers.ssn"},
        "n": {"public.customers.name"},
    }


def test_unparseable_sql_yields_nothing_to_guess_from() -> None:
    assert _access("SELEC nonsense").columns == {}


# --------------------------------------------------------------------------
# From column access to governance
# --------------------------------------------------------------------------

from typing import Any  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from interlock.core.policy import PolicyEngine  # noqa: E402
from interlock.core.source_roles import (  # noqa: E402
    PermissionStatement,
    SourceRoleEvaluator,
)
from interlock.core.sql_governance import naming_for  # noqa: E402
from interlock.gateway.mcp_adapter import redact_policy_columns  # noqa: E402
from interlock.gateway.pipeline import GatewayPipeline, sql_gateway_request  # noqa: E402
from interlock.models import AgentType, IdentityContext, PolicyRule  # noqa: E402

NAMING = naming_for(
    "postgresql",
    {},
    {"effective_schemas": ["public"]},
    frozenset(CATALOG),
    CATALOG,
)


class _Naming:
    async def naming_for_source(self, source_id: str) -> Any:
        return NAMING


async def _request(sql: str) -> Any:
    pipeline = GatewayPipeline(sql_naming=_Naming())
    view = await pipeline.governance_view(
        sql_gateway_request(sql, protocol="mcp", source_id="s", dialect="postgres"),
        identity_id=1,
    )
    return view


def _stmt(effect: str, pattern: str, resource_type: str = "db.column", pid: int = 1) -> Any:
    return PermissionStatement(
        permission_id=pid,
        role_id=1,
        role_key="r",
        effect=effect,
        action="db.table.select",
        resource_type=resource_type,
        resource_pattern=pattern,
        constraints={},
    )


async def _evaluate(request: Any, *statements: Any) -> Any:
    evaluator = SourceRoleEvaluator(MagicMock())
    evaluator._load_statements = AsyncMock(return_value=list(statements))  # type: ignore[method-assign]
    return await evaluator.evaluate(request)


async def test_a_request_carries_one_resource_per_column_read() -> None:
    view = await _request("SELECT name FROM customers WHERE id = 1")
    assert sorted(view.permission_request.resources) == [
        "public.customers.id",
        "public.customers.name",
    ]


async def test_a_column_deny_fires_on_every_path_to_the_column() -> None:
    for sql in (
        "SELECT ssn FROM customers",
        "SELECT * FROM customers",
        "SELECT name FROM customers WHERE ssn = 'x'",
    ):
        view = await _request(sql)
        decision = await _evaluate(
            view.permission_request,
            _stmt("allow", "*.*", "db.table"),
            _stmt("deny", "public.customers.ssn", pid=2),
        )
        assert not decision.allowed, sql


async def test_a_gap_fails_closed_only_where_column_rules_apply() -> None:
    view = await _request("SELECT to_jsonb(c) FROM customers c")
    assert "public.customers" in view.permission_request.column_gaps
    refused = await _evaluate(
        view.permission_request,
        _stmt("allow", "*.*", "db.table"),
        _stmt("deny", "public.customers.ssn", pid=2),
    )
    assert not refused.allowed and "could not be determined" in refused.reason
    # A wildcard-only identity is unaffected: the table is read as a whole.
    assert (await _evaluate(view.permission_request, _stmt("allow", "*.*", "db.table"))).allowed


async def test_a_column_allow_needs_every_column_and_count_needs_the_table() -> None:
    allows = (_stmt("allow", "public.customers.id"), _stmt("allow", "public.customers.name", pid=2))
    ok = await _request("SELECT name FROM customers WHERE id = 1")
    assert (await _evaluate(ok.permission_request, *allows)).allowed
    for sql in ("SELECT email FROM customers", "SELECT count(*) FROM customers"):
        view = await _request(sql)
        assert not (await _evaluate(view.permission_request, *allows)).allowed, sql


async def test_wildcard_roles_keep_working() -> None:
    view = await _request(
        "SELECT c.name, o.total FROM customers c JOIN orders o ON o.customer_id = c.id"
    )
    for pattern in ("*.*", "public.*", "*"):
        decision = await _evaluate(view.permission_request, _stmt("allow", pattern, "db.table"))
        assert decision.allowed, pattern


async def test_a_deny_policy_on_a_column_now_fires() -> None:
    engine = PolicyEngine(MagicMock())
    engine._rules = [
        PolicyRule(
            id=1,
            name="deny-ssn",
            priority=90,
            conditions={"columns": ["ssn"]},
            actions={"effect": "deny"},
        ),
        PolicyRule(id=2, name="allow", priority=1, conditions={}, actions={"effect": "allow"}),
    ]
    identity = IdentityContext(identity_id=1, user="a", agent_type=AgentType.CUSTOM)
    for sql, denied in (("SELECT ssn FROM customers", True), ("SELECT name FROM customers", False)):
        view = await _request(sql)
        decision = engine.evaluate(
            identity,
            "s",
            view.operation,
            tables=view.tables,
            deny_column_spellings=view.deny_column_spellings,
        )
        assert decision.allowed is not denied, sql


def test_redaction_follows_lineage_and_falls_back_to_names() -> None:
    rows = [{"tax_id": "123-45-6789", "loud": "123-45-6789", "name": "Ada", "ssn": "x"}]
    lineage = {
        "tax_id": {"public.customers.ssn"},
        "loud": {"public.customers.ssn"},
        "name": {"public.customers.name"},
    }
    redacted, stats = redact_policy_columns(rows, ["customers.ssn"], lineage)
    assert redacted[0]["tax_id"] == redacted[0]["loud"] == "[REDACTED:POLICY]"
    assert redacted[0]["name"] == "Ada"
    # `ssn` has no lineage entry here, so its own name decides.
    assert redacted[0]["ssn"] == "[REDACTED:POLICY]"
    assert stats["count"] == 3
