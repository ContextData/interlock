"""Literal admin routes must not be swallowed by parameterised siblings.

The console's source-role "Dry-run / explain" panel posts to
`/dashboard/data-sources/{source_id}/roles/dry-run`, and
`/dashboard/data-sources/{source_id}/roles/{role_id}` was registered first with
an unconstrained parameter. Starlette matched `dry-run` as `role_id`, FastAPI
answered `422 unable to parse dry-run as an integer`, and the whole explain
feature was dead in the shipped product - the only surface that answers "what
can this identity actually do on this source", which the operator guide points
auditors at. No test covered the path, so nothing noticed.
"""

from __future__ import annotations

from starlette.routing import Match

from interlock.admin.app import create_app


def _first_full_match(app, method: str, path: str) -> str | None:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "path_params": {},
    }
    for route in app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return getattr(route, "name", None)
    return None


def test_the_source_role_dry_run_path_reaches_its_own_handler() -> None:
    app = create_app()

    assert (
        _first_full_match(app, "POST", "/dashboard/data-sources/sales_pg/roles/dry-run")
        == "dry_run_source_role"
    )


def test_a_numeric_role_id_still_reaches_the_update_handler() -> None:
    """The fix constrains the parameter; it must not break the route it guards."""
    app = create_app()

    assert (
        _first_full_match(app, "POST", "/dashboard/data-sources/sales_pg/roles/42")
        == "update_source_role"
    )


def test_the_statement_row_path_reaches_its_own_handler() -> None:
    app = create_app()

    assert (
        _first_full_match(app, "GET", "/dashboard/data-sources/sales_pg/roles/statement-row")
        == "source_role_statement_row"
    )


def test_the_role_delete_path_reaches_its_own_handler() -> None:
    app = create_app()

    assert (
        _first_full_match(app, "POST", "/dashboard/data-sources/sales_pg/roles/42/delete")
        == "delete_source_role"
    )
