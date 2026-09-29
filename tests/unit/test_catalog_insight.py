"""Drift exposure and access analytics, from their pure parts."""

from __future__ import annotations

from interlock.catalog.analytics import summarise
from interlock.catalog.exposure import exposing_roles
from interlock.core.source_roles import PermissionStatement


def _stmt(
    role: str, effect: str, pattern: str, resource_type: str = "db.table"
) -> PermissionStatement:
    return PermissionStatement(
        permission_id=hash((role, effect, pattern)) % 10_000,
        role_id=hash(role) % 1000,
        role_key=role,
        effect=effect,
        action="db.table.select",
        resource_type=resource_type,
        resource_pattern=pattern,
        constraints={},
    )


STATEMENTS = [
    _stmt("analyst", "allow", "sales.*"),
    _stmt("everything", "allow", "*.*"),
    _stmt("guarded", "allow", "*.*"),
    _stmt("guarded", "deny", "sales.payroll.*"),
    _stmt("hr_only", "allow", "hr.*"),
]


def test_a_new_table_is_exposed_by_every_role_whose_patterns_cover_it() -> None:
    roles = exposing_roles(STATEMENTS, node_type="table", path=["sales", "payroll"])
    assert roles == ["analyst", "everything"]


def test_a_role_that_denies_the_new_object_does_not_expose_it() -> None:
    assert "guarded" not in exposing_roles(STATEMENTS, node_type="table", path=["sales", "payroll"])
    assert "guarded" in exposing_roles(STATEMENTS, node_type="table", path=["sales", "orders"])


def test_a_new_column_is_evaluated_as_a_column() -> None:
    roles = exposing_roles(
        [*STATEMENTS, _stmt("cols", "allow", "hr.people.name", "db.column")],
        node_type="column",
        path=["hr", "people", "ssn"],
    )
    assert "cols" not in roles and "hr_only" in roles


def _row(resource: str, status: str, requests: int, identities: int = 1) -> dict[str, object]:
    return {"resource": resource, "status": status, "requests": requests, "identities": identities}


def test_analytics_rank_tables_and_find_pii_and_unused_tables() -> None:
    result = summarise(
        source_id="s",
        days=30,
        resource_rows=[
            _row("sales.customers.email", "success", 5, 2),
            _row("sales.customers.name", "success", 3),
            _row("sales.orders.*", "success", 2),
            _row("sales.payroll.*", "denied", 4),
            _row("*.*", "success", 9),
        ],
        catalog_rows=[
            {"resource_key": "sales.customers", "node_type": "table"},
            {"resource_key": "sales.orders", "node_type": "table"},
            {"resource_key": "sales.payroll", "node_type": "table"},
            {"resource_key": "sales.archive", "node_type": "table"},
            {"resource_key": "sales.customers.email", "node_type": "column"},
            {"resource_key": "sales.customers.name", "node_type": "column"},
        ],
        annotations={"sales.customers.name": "sensitive"},
    )
    assert [t.table for t in result.most_accessed] == ["sales.customers", "sales.orders"]
    assert result.most_accessed[0].requests == 8
    assert ("sales.customers.email", 5, "likely") in result.pii_columns
    assert ("sales.customers.name", 3, "sensitive") in result.pii_columns
    assert [t.table for t in result.denied] == ["sales.payroll"]
    assert result.never_accessed == ["sales.archive"]
    assert result.requests == 23
