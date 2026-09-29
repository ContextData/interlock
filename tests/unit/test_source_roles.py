from __future__ import annotations

import pytest

from interlock.core.source_roles import SourceRoleEvaluator, build_permission_request


class FakePool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


def stmt(
    *,
    permission_id: int,
    role_id: int = 1,
    role_key: str = "test",
    effect: str = "allow",
    action: str = "db.table.select",
    resource_type: str = "db.table",
    resource_pattern: str = "public.customers.*",
    constraints: dict | None = None,
):
    return {
        "permission_id": permission_id,
        "role_id": role_id,
        "role_key": role_key,
        "effect": effect,
        "action": action,
        "resource_type": resource_type,
        "resource_pattern": resource_pattern,
        "constraints": constraints or {},
    }


@pytest.mark.asyncio
async def test_source_role_default_denies_when_no_permission_matches():
    evaluator = SourceRoleEvaluator(FakePool([]))
    request = build_permission_request(
        protocol="postgresql",
        source_id="pg",
        identity_id=10,
        operation="SELECT",
        tables=["public.customers"],
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is False
    assert decision.missing_permissions


@pytest.mark.asyncio
async def test_source_role_explicit_deny_wins_over_allow():
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                stmt(permission_id=1, effect="allow"),
                stmt(permission_id=2, effect="deny", role_id=2, role_key="blocked"),
            ]
        )
    )
    request = build_permission_request(
        protocol="postgresql",
        source_id="pg",
        identity_id=10,
        operation="SELECT",
        tables=["public.customers"],
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is False
    assert decision.explicit_deny is True
    assert decision.matched_role_ids == [2]


@pytest.mark.asyncio
async def test_source_role_unions_multiple_roles_for_resources():
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                stmt(
                    permission_id=1,
                    role_id=1,
                    role_key="customer_reader",
                    resource_pattern="public.customers.*",
                ),
                stmt(
                    permission_id=2,
                    role_id=2,
                    role_key="order_reader",
                    resource_pattern="public.orders.*",
                ),
            ]
        )
    )
    request = build_permission_request(
        protocol="postgresql",
        source_id="pg",
        identity_id=10,
        operation="SELECT",
        tables=["public.customers", "public.orders"],
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is True
    assert decision.matched_role_ids == [1, 2]


@pytest.mark.asyncio
async def test_source_role_tableless_select_matches_database_read_role():
    evaluator = SourceRoleEvaluator(FakePool([stmt(permission_id=1, resource_pattern="*.*")]))
    request = build_permission_request(
        protocol="postgresql",
        source_id="pg",
        identity_id=10,
        operation="SELECT",
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is True
    assert request.resources == ["*.*"]


@pytest.mark.asyncio
async def test_source_role_http_method_and_path_template_match():
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                stmt(
                    permission_id=1,
                    action="http.get",
                    resource_type="http.path",
                    resource_pattern="/customers/{id}",
                )
            ]
        )
    )
    request = build_permission_request(
        protocol="http",
        source_id="api",
        identity_id=10,
        operation="read",
        method="GET",
        path="/customers/123",
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is True
    assert decision.matched_permission_ids == [1]


@pytest.mark.asyncio
async def test_source_role_insert_requires_distinct_write_action():
    evaluator = SourceRoleEvaluator(FakePool([stmt(permission_id=1, action="db.table.select")]))
    request = build_permission_request(
        protocol="postgresql",
        source_id="pg",
        identity_id=10,
        operation="INSERT",
        tables=["public.customers"],
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is False
    assert decision.missing_permissions[0]["action"] == "db.table.insert"


@pytest.mark.asyncio
async def test_source_role_delete_constraint_requires_where_clause():
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                stmt(
                    permission_id=1,
                    action="db.table.delete_rows",
                    constraints={"require_where": True},
                )
            ]
        )
    )
    request = build_permission_request(
        protocol="postgresql",
        source_id="pg",
        identity_id=10,
        operation="DELETE",
        tables=["public.customers"],
        metadata={"has_where": False},
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is False
    assert decision.missing_permissions[0]["action"] == "db.table.delete_rows"

    request.metadata["has_where"] = True
    decision = await evaluator.evaluate(request)

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_metadata_resource_type_cannot_broaden_permission():
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                stmt(
                    permission_id=1,
                    action="db.table.select",
                    resource_type="http.path",
                    resource_pattern="*",
                )
            ]
        )
    )
    request = build_permission_request(
        protocol="postgresql",
        source_id="pg",
        identity_id=10,
        operation="SELECT",
        tables=["public.customers"],
        metadata={"resource_type": "http.path"},
    )

    decision = await evaluator.evaluate(request)

    assert request.resource_type == "db.table"
    assert decision.allowed is False
    assert decision.missing_permissions[0]["resource_type"] == "db.table"


def test_mcp_describe_source_builds_describe_permission_request():
    request = build_permission_request(
        protocol="mcp",
        source_id="pg",
        identity_id=10,
        operation="read",
        metadata={"tool_name": "agentgate_describe_source"},
    )

    assert request.action == "db.table.describe"
    assert request.resource_type == "db.table"
    assert request.resources == ["*.*"]


def test_mcp_related_documents_builds_discovery_asset_read_request():
    request = build_permission_request(
        protocol="mcp",
        source_id="pg",
        identity_id=10,
        operation="discovery",
        path="assets/42",
        metadata={"tool_name": "agentgate_related_documents"},
    )

    assert request.action == "discovery.asset.read"
    assert request.resource_type == "discovery.asset"
    assert request.resources == ["*"]


@pytest.mark.asyncio
async def test_describe_access_reports_what_evaluate_would_decide_from() -> None:
    """An agent should be able to plan instead of discovering limits by refusal.

    The summary is built from the same statements `evaluate` reads, so the two
    cannot drift: whatever this promises is what enforcement will consult.
    """
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                stmt(permission_id=1, role_key="analyst", action="db.table.select"),
                stmt(
                    permission_id=2,
                    role_key="analyst",
                    action="db.table.describe",
                    resource_pattern="*.*",
                ),
                stmt(
                    permission_id=3,
                    role_key="guardrail",
                    effect="deny",
                    action="db.table.delete_rows",
                    resource_pattern="*.*",
                ),
            ]
        )
    )

    access = await evaluator.describe_access(7, "sales_pg")

    assert access["roles"] == ["analyst", "guardrail"]
    assert {entry["action"] for entry in access["allow"]} == {
        "db.table.select",
        "db.table.describe",
    }
    # A deny is the single most useful thing to know before attempting anything.
    assert access["deny"] == [{"action": "db.table.delete_rows", "resource": "*.*"}]


@pytest.mark.asyncio
async def test_describe_access_on_an_ungranted_source_promises_nothing() -> None:
    """No grants means no statements, and the answer must not imply authority."""
    access = await SourceRoleEvaluator(FakePool([])).describe_access(7, "sales_pg")

    assert access == {"roles": [], "allow": [], "deny": []}
