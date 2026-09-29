"""Render tests for the source catalog's admin views.

The Catalog section on source detail, the lazy tree, the classification
control and the cross-source search page, rendered in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from interlock.admin.app import _format_datetime
from interlock.catalog.read import (
    CatalogNodeView,
    CatalogOverview,
    ChangeView,
    ScanSummary,
    SourceCatalogStatus,
)

_TEMPLATES = Path("src/interlock/admin/templates")
_WHEN = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
CLASSIFICATIONS = ("pii", "sensitive", "not_pii", "public")


def _render(name: str, **ctx: Any) -> str:
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=select_autoescape(["html"]))
    env.filters["fmt_dt"] = _format_datetime
    return env.get_template(name).render(content_only=True, **ctx)


def _scan(**overrides: Any) -> ScanSummary:
    values: dict[str, Any] = {
        "id": 7,
        "trigger": "save",
        "status": "succeeded",
        "requested_by": "admin",
        "requested_at": _WHEN,
        "started_at": _WHEN,
        "finished_at": _WHEN,
        "nodes_seen": 12,
        "added": 0,
        "removed": 0,
        "changed": 0,
        "truncated": False,
        "truncation": {},
        "baseline": True,
        "error_code": None,
        "error_message": None,
    }
    values.update(overrides)
    return ScanSummary(**values)


def _node(path: list[str], node_type: str, **overrides: Any) -> CatalogNodeView:
    values: dict[str, Any] = {
        "id": 1,
        "node_type": node_type,
        "path": path,
        "name": path[-1],
        "resource_key": ".".join(path).lower(),
        "data_type": None,
        "heuristic_class": None,
        "attributes": {},
        "removed": False,
        "last_seen_at": _WHEN,
        "source_id": "sales_pg",
    }
    values.update(overrides)
    return CatalogNodeView(**values)


def _section(**overrides: Any) -> str:
    status = SourceCatalogStatus(
        source_id="sales_pg",
        collectable=True,
        enabled=True,
        last_scan=_scan(),
        last_success=_scan(),
        counts={"schema": 2, "table": 3, "column": 9, "source": 1},
        source_attributes={"default_schema": "sales", "login": "reader"},
    )
    ctx: dict[str, Any] = {
        "source_id": "sales_pg",
        "status": status,
        "enforced": True,
        "nodes": [_node(["sales"], "schema", child_count=3)],
        "changes": [],
        "history": [_scan()],
        "classifications": CLASSIFICATIONS,
        "notice": "",
    }
    ctx.update(overrides)
    return _render("partials/catalog_section.html", **ctx)


def test_section_shows_the_last_scan_counts_and_scan_facts() -> None:
    html = _section()
    assert 'hx-post="/dashboard/data-sources/sales_pg/catalog/rescan"' in html
    assert "succeeded" in html
    assert "3 tables" in html and "9 columns" in html and "2 schemas" in html
    assert "1 source" not in html
    assert "<code>sales</code>" in html
    assert "<code>reader</code>" in html
    assert "Not enforced on agent paths yet" not in html


def test_a_non_sql_source_is_labelled_not_enforced() -> None:
    assert "Not enforced on agent paths yet" in _section(enforced=False)


def test_a_source_without_a_collector_says_so_and_offers_no_rescan() -> None:
    status = SourceCatalogStatus(source_id="team_slack", collectable=False, enabled=True)
    html = _section(status=status, source_id="team_slack")
    assert "cannot record this connector" in html
    assert "catalog/rescan" not in html


def test_a_truncated_or_failed_scan_is_explained() -> None:
    failed = _scan(status="failed", error_code="auth", error_message="password rejected")
    truncated = _scan(truncated=True, truncation={"max_tables": 20000})
    status = SourceCatalogStatus(
        source_id="sales_pg",
        collectable=True,
        enabled=True,
        last_scan=failed,
        last_success=truncated,
    )
    html = _section(status=status)
    assert "<code>auth</code>" in html and "password rejected" in html
    assert "truncated" in html and "max_tables" in html


def test_drift_lists_changes_with_per_row_and_bulk_acknowledge() -> None:
    change = ChangeView(
        id=41,
        scan_id=8,
        node_type="column",
        path=["sales", "customers", "tier"],
        change="changed",
        before={"data_type": "integer"},
        after={"data_type": "text"},
        created_at=_WHEN,
        acknowledged_at=None,
        acknowledged_by=None,
    )
    html = _section(changes=[change])
    assert "sales.customers.tier" in html
    assert "<code>integer</code> to <code>text</code>" in html
    assert 'name="change_id" value="41"' in html
    assert "Acknowledge all" in html


def test_the_tree_loads_children_lazily_with_the_path_as_json() -> None:
    html = _render(
        "partials/catalog_children.html",
        source_id="sales_pg",
        nodes=[_node(["sales", "odd.table"], "table", child_count=2)],
        classifications=CLASSIFICATIONS,
    )
    assert 'hx-trigger="toggle once"' in html
    # The path travels as JSON, so a dot inside an identifier stays inside it.
    assert "path=%5B%22sales%22%2C%22odd.table%22%5D" in html


def test_a_column_carries_its_classification_control() -> None:
    column = _node(
        ["sales", "customers", "email"],
        "column",
        data_type="text",
        heuristic_class="free_text",
        classification="pii",
        classification_source="wizard",
    )
    html = _render(
        "partials/catalog_children.html",
        source_id="sales_pg",
        nodes=[column],
        classifications=CLASSIFICATIONS,
    )
    assert 'hx-post="/dashboard/data-sources/sales_pg/catalog/annotations"' in html
    assert 'name="path" value="[&#34;sales&#34;,&#34;customers&#34;,&#34;email&#34;]"' in html
    assert '<option value="pii" selected>' in html
    assert "set by wizard" in html
    # An admin's or wizard's decision replaces the name heuristic's hint.
    assert "likely PII" not in html


def test_search_results_link_to_the_source_and_note_the_limit() -> None:
    results = [
        _node(["sales", "customers", "ssn"], "column", heuristic_class="free_text"),
        _node(["sales", "customers", "tier"], "column", heuristic_class="free_text"),
    ]
    html = _render("partials/catalog_results.html", results=results, result_limit=2)
    assert 'href="/dashboard/data-sources/sales_pg"' in html
    assert "sales.customers.ssn" in html
    # Only the column whose name suggests personal data is flagged.
    assert html.count("likely PII") == 1
    assert "Showing the first 2 matches" in html


def test_catalog_page_renders_overview_and_filters() -> None:
    overview = CatalogOverview(
        sources_scanned=2,
        succeeded=1,
        failed=1,
        pending=1,
        running=0,
        tables=4,
        columns=20,
        last_finished_at=_WHEN,
        failures=[("hr_mysql", "auth")],
    )
    html = _render(
        "pages/catalog.html",
        overview=overview,
        q="cust",
        source="",
        node_type="column",
        classification="pii_any",
        results=[],
        result_limit=200,
        sources=["sales_pg", "hr_mysql"],
        node_types=("table", "column"),
    )
    assert "1/2 scanned successfully" in html and "1 failing" in html
    assert "4 tables and views, 20 columns" in html
    assert 'hx-get="/dashboard/catalog"' in html
    assert '<option value="column" selected>' in html
    assert '<option value="pii_any" selected>' in html
    assert "No catalogued objects match" in html


def test_the_source_role_dry_run_shows_other_spellings_and_resolution() -> None:
    html = _render(
        "partials/source_role_dry_run_result.html",
        error=None,
        decision={
            "allowed": True,
            "reason": "allowed",
            "matched_role_ids": [1],
            "matched_permission_ids": [2],
            "missing_permissions": [],
        },
        permission_request={
            "action": "db.table.select",
            "resources": ["shop.customers.*"],
            "resource_variants": {
                "shop.customers.*": {"aliases": ["public.customers.*"]},
            },
            "naming_resolved": True,
        },
    )
    assert "is also matched as <code>public.customers.*</code>" in html
    assert "Resolved from the source catalog." in html


def test_pick_mode_offers_use_buttons_instead_of_classifications() -> None:
    column = _node(["sales", "customers", "email"], "column", resource_key="sales.customers.email")
    table = _node(["sales", "customers"], "table", resource_key="sales.customers", child_count=3)
    html = _render(
        "partials/catalog_children.html",
        source_id="sales_pg",
        nodes=[table, column],
        classifications=CLASSIFICATIONS,
        pick=True,
    )
    assert 'data-pattern="sales.customers.*"' in html
    assert 'data-pattern="sales.customers.email"' in html
    assert 'data-column="customers.email"' in html
    assert 'data-table="sales.customers"' in html
    assert 'data-resource-type="db.column"' in html
    assert "pick=1" in html
    assert "catalog/annotations" not in html


def test_lint_warnings_render_with_their_statement() -> None:
    from interlock.catalog.validation import LintWarning

    html = _render(
        "partials/catalog_lint.html",
        warnings=[LintWarning(code="matches_nothing", message="x matches nothing", index=1)],
        checked=True,
    )
    assert "statement 2" in html and "x matches nothing" in html
    assert "No warnings" in _render("partials/catalog_lint.html", warnings=[], checked=True)


def test_drift_shows_who_can_already_read_an_added_table() -> None:
    change = ChangeView(
        id=7,
        scan_id=3,
        node_type="table",
        path=["sales", "payroll"],
        change="added",
        before={},
        after={},
        created_at=_WHEN,
        acknowledged_at=None,
        acknowledged_by=None,
        exposure={"roles": ["analyst", "everything"], "identities": 4},
    )
    html = _section(changes=[change])
    assert "4 identities" in html and "via analyst, everything" in html


def test_the_analytics_page_lists_the_four_views() -> None:
    from interlock.catalog.analytics import AccessAnalytics, TableAccess

    analytics = AccessAnalytics(
        source_id="sales_pg",
        days=30,
        most_accessed=[TableAccess(table="sales.customers", requests=9, denied=1, identities=2)],
        pii_columns=[("sales.customers.ssn", 3, "pii")],
        never_accessed=["sales.archive"],
        denied=[TableAccess(table="sales.payroll", requests=4, denied=4)],
        requests=13,
    )
    html = _render(
        "pages/catalog_analytics.html",
        analytics=analytics,
        sources=["sales_pg"],
        source="sales_pg",
        days=30,
    )
    assert "<code>sales.customers</code></td><td>8</td>" in html
    assert "sales.customers.ssn" in html and "sales.archive" in html
    assert "<code>sales.payroll</code></td><td>4</td>" in html
