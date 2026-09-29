"""Catalog warnings for role statements and policy rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from interlock.catalog.validation import CatalogView, lint_policy, lint_statements
from interlock.core.sql_governance import naming_for


def _view(connector: str = "postgresql", **kwargs: object) -> CatalogView:
    values: dict[str, object] = {
        "source_id": "s",
        "connector_key": connector,
        "collectable": True,
        "naming": naming_for(connector, {"database": "shop"}),
        "tables": {
            "sales.customers": ["id", "email", "ssn"],
            "hr.customers": ["id"],
            "sales.orders": ["id", "total"],
        },
        "scanned_at": datetime.now(UTC) - timedelta(hours=3),
    }
    values.update(kwargs)
    return CatalogView(**values)  # type: ignore[arg-type]


def _stmt(pattern: str, effect: str = "allow", resource_type: str = "db.table") -> dict[str, str]:
    return {
        "effect": effect,
        "action": "db.table.select",
        "resource_type": resource_type,
        "resource_pattern": pattern,
    }


def _codes(warnings: list) -> list[str]:  # type: ignore[type-arg]
    return [w.code for w in warnings]


def test_a_pattern_that_matches_nothing_is_flagged_with_the_scan_age() -> None:
    (warning,) = lint_statements(_view(), [_stmt("sales.invoices.*")])
    assert warning.code == "matches_nothing"
    assert "3 hours ago" in warning.message
    assert warning.index == 0


def test_patterns_that_match_are_quiet() -> None:
    assert (
        lint_statements(_view(), [_stmt("*.*"), _stmt("sales.*"), _stmt("sales.customers.*")]) == []
    )


def test_a_column_allow_explains_what_it_costs() -> None:
    warnings = lint_statements(_view(), [_stmt("sales.customers.email", resource_type="db.column")])
    assert _codes(warnings) == ["column_scoped"]
    assert "SELECT *" in warnings[0].message


def test_a_column_deny_is_not_a_cost_warning() -> None:
    assert lint_statements(_view(), [_stmt("sales.customers.ssn", effect="deny")]) == []


def test_a_legacy_public_pattern_on_mysql_is_flagged() -> None:
    view = _view("mysql", tables={"shop.customers": ["id"]})
    assert "legacy_alias" in _codes(lint_statements(view, [_stmt("public.customers.*")]))


def test_non_sql_patterns_are_labelled_not_enforced() -> None:
    view = _view("s3", naming=naming_for("s3"), tables={})
    assert _codes(lint_statements(view, [_stmt("s3://bucket/*")])) == ["not_enforced"]


def test_http_patterns_are_not_labelled_not_enforced() -> None:
    view = _view("generic_rest", naming=naming_for("generic_rest"), tables={}, scanned_at=None)
    assert "not_enforced" not in _codes(lint_statements(view, [_stmt("/users/*")]))


def test_an_unscanned_sql_source_says_nothing_can_be_checked() -> None:
    assert "not_scanned" in _codes(lint_statements(_view(scanned_at=None), [_stmt("x.y.*")]))


def test_policy_names_are_checked_against_the_catalog() -> None:
    warnings = lint_policy(
        _view(),
        tables=["customers", "sales.invoices", "orders"],
        columns=["nope"],
        redact_columns=["customers.ssn"],
        effect="deny",
    )
    codes = _codes(warnings)
    assert "ambiguous_table" in codes  # customers is in sales and hr
    assert "unknown_table" in codes  # sales.invoices
    assert "unknown_column" in codes  # nope
    assert "redact_on_deny" in codes


def test_an_allow_policy_with_columns_is_told_it_never_matches() -> None:
    warnings = lint_policy(None, tables=[], columns=["ssn"], redact_columns=[], effect="allow")
    assert _codes(warnings) == ["allow_columns_never_match"]
