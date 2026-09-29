"""Render tests for Phase 4 Discovery dashboard pages.

These cover the template contracts that the HTMX flows depend on:

- /dashboard/discovery shows stats, unified search box, three tabs,
  and the Rescan Sources button.
- partials/discovery_tab.html renders Assets, Entities, and Categories
  variants with the right click-through links.
- partials/discovery_search_results.html shows fallback messaging when
  the semantic engine is unavailable.
- Detail pages (asset, entity, category) include the navigation
  affordances that the JS-free row clicks rely on.

We render in isolation (no Starlette / TestClient) so these stay fast
and free of DB/Redis dependencies.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES = Path("src/interlock/admin/templates")


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )


def _render(name: str, **ctx) -> str:
    return _env().get_template(name).render(content_only=True, **ctx)


# ---------------------------------------------------------------------------
# pages/discovery.html
# ---------------------------------------------------------------------------


def _page_ctx(**overrides):
    base = {
        "active_page": "discovery",
        "active_tab": "assets",
        "stats": {
            "assets": 12,
            "entities": 34,
            "categories": 5,
            "sources": 2,
        },
        "last_run": None,
        "assets": [],
        "entities": [],
        "categories": [],
        "q": "",
    }
    base.update(overrides)
    return base


def test_discovery_page_renders_stats_and_rescan_button() -> None:
    html = _render("pages/discovery.html", **_page_ctx())
    assert "Discovery" in html
    assert "Rescan Sources" in html
    # All four stats appear with their counts.
    for num in ("12", "34", "5", "2"):
        assert f">{num}<" in html
    # The unified search input must hx-get the search endpoint.
    assert 'hx-get="/dashboard/discovery/search"' in html
    # All three tabs are rendered.
    assert "?tab=assets" in html
    assert "?tab=entities" in html
    assert "?tab=categories" in html
    # Rescan button targets the right partial container.
    assert 'hx-post="/dashboard/discovery/rescan"' in html


def _overview(**overrides: object) -> object:
    from datetime import UTC, datetime

    from interlock.catalog.read import CatalogOverview

    values: dict[str, object] = {
        "sources_scanned": 3,
        "succeeded": 2,
        "failed": 1,
        "pending": 0,
        "running": 0,
        "tables": 14,
        "columns": 88,
        "last_finished_at": datetime(2026, 5, 15, 1, 0, 10, tzinfo=UTC),
        "failures": [("main-pg", "auth")],
    }
    values.update(overrides)
    return CatalogOverview(**values)  # type: ignore[arg-type]


def test_discovery_page_includes_catalog_summary_when_present() -> None:
    html = _render("pages/discovery.html", **_page_ctx(last_run=_overview()))
    assert "2/3 sources scanned successfully" in html
    assert "14 tables and 88 columns" in html
    assert "main-pg: auth" in html


# ---------------------------------------------------------------------------
# partials/discovery_tab.html
# ---------------------------------------------------------------------------


def test_discovery_tab_assets_links_to_asset_detail() -> None:
    assets = [
        {
            "id": 7,
            "source_id": "main-pg",
            "asset_type": "table",
            "asset_path": "public.orders",
            "title": "orders",
            "summary": "{}",
            "category_path": "main_pg.public.orders",
            "updated_at": "2026-05-15T01:00:00+00:00",
        }
    ]
    html = _render(
        "partials/discovery_tab.html",
        active_tab="assets",
        assets=assets,
        entities=[],
        categories=[],
        q="",
    )
    assert "/dashboard/discovery/assets/7" in html
    assert "orders" in html
    assert "public.orders" in html


def test_discovery_tab_entities_links_to_entity_detail() -> None:
    entities = [
        {"text": "user_id", "entity_type": "identifier", "doc_count": 3},
    ]
    html = _render(
        "partials/discovery_tab.html",
        active_tab="entities",
        assets=[],
        entities=entities,
        categories=[],
        q="",
    )
    assert "/dashboard/discovery/entities/identifier/user_id" in html
    assert "user_id" in html
    assert "disc-badge-identifier" in html


def test_discovery_tab_categories_links_to_category_detail() -> None:
    cats = [
        {
            "id": 1,
            "name": "Public",
            "path": "actualize.public",
            "doc_count": 9,
            "has_children": True,
        }
    ]
    html = _render(
        "partials/discovery_tab.html",
        active_tab="categories",
        assets=[],
        entities=[],
        categories=cats,
        q="",
    )
    assert "/dashboard/discovery/categories/actualize.public" in html
    assert "Public" in html
    assert "has children" in html


def test_discovery_tab_empty_state_for_each_variant() -> None:
    for tab in ("assets", "entities", "categories"):
        html = _render(
            "partials/discovery_tab.html",
            active_tab=tab,
            assets=[],
            entities=[],
            categories=[],
            q="",
        )
        assert "disc-empty" in html


# ---------------------------------------------------------------------------
# partials/discovery_search_results.html
# ---------------------------------------------------------------------------


def test_discovery_search_results_show_count_and_links() -> None:
    results = [
        {
            "asset_id": 11,
            "source_id": "main-pg",
            "asset_type": "table",
            "asset_path": "public.users",
            "title": "users",
            "summary": "users table",
            "score": 1.23,
            "strategy": "vector",
        }
    ]
    html = _render(
        "partials/discovery_search_results.html",
        q="user",
        results=results,
        engine_available=True,
    )
    assert "1 result" in html
    assert "/dashboard/discovery/assets/11" in html
    assert "vector" in html
    assert "RRF 1.2300" in html


def test_discovery_search_results_warns_when_engine_unavailable() -> None:
    html = _render(
        "partials/discovery_search_results.html",
        q="user",
        results=[
            {
                "asset_id": 11,
                "source_id": "main-pg",
                "asset_type": "table",
                "asset_path": "public.users",
                "title": "users",
                "summary": "",
                "score": 0.0,
                "strategy": "fallback",
            }
        ],
        engine_available=False,
    )
    assert "semantic engine unavailable" in html


def test_discovery_search_results_empty_for_no_query() -> None:
    html = _render(
        "partials/discovery_search_results.html",
        q="",
        results=[],
        engine_available=True,
    )
    # No query -> the partial renders nothing (no "result" word).
    assert "No results" not in html
    assert "result" not in html.lower()


# ---------------------------------------------------------------------------
# pages/discovery_asset_detail.html
# ---------------------------------------------------------------------------


def test_asset_detail_shows_columns_and_related_links() -> None:
    asset = {
        "id": 7,
        "source_id": "main-pg",
        "asset_type": "table",
        "asset_path": "public.orders",
        "title": "orders",
        "summary": {},
        "category_path": "main_pg.public.orders",
        "topics": ["orders", "public"],
        "quality_score": 0.95,
        "updated_at": "2026-05-15T01:00:00+00:00",
        "created_at": "2026-05-15T00:00:00+00:00",
    }
    columns = [
        {
            "ordinal_position": 1,
            "column_name": "id",
            "data_type": "integer",
            "is_nullable": "NO",
            "column_default": "nextval(...)",
        },
        {
            "ordinal_position": 2,
            "column_name": "user_email",
            "data_type": "text",
            "is_nullable": "YES",
            "column_default": None,
        },
    ]
    entities = [
        {"entity_text": "user_email", "entity_type": "free_text", "prominence": 1.0},
    ]
    related = [
        {
            "asset_id": 8,
            "source_id": "main-pg",
            "asset_type": "table",
            "asset_path": "public.invoices",
            "title": "invoices",
            "shared_entities": 3,
        },
    ]
    html = _render(
        "pages/discovery_asset_detail.html",
        asset=asset,
        columns=columns,
        row_estimate=1234,
        entities=entities,
        related=related,
    )
    assert "orders" in html
    # Category link works.
    assert "/dashboard/discovery/categories/main_pg.public.orders" in html
    # Each column row appears.
    assert "user_email" in html
    assert "integer" in html
    # Entity links into entity detail.
    assert "/dashboard/discovery/entities/free_text/user_email" in html
    # Related asset link.
    assert "/dashboard/discovery/assets/8" in html
    assert "1234" in html


# ---------------------------------------------------------------------------
# pages/discovery_entity_detail.html
# ---------------------------------------------------------------------------


def test_entity_detail_lists_assets_and_co_occurring() -> None:
    assets = [
        {
            "asset_id": 7,
            "source_id": "main-pg",
            "asset_type": "table",
            "asset_path": "public.orders",
            "title": "orders",
            "category_path": "main_pg.public.orders",
            "prominence": 1.0,
        }
    ]
    co = [
        {
            "entity_text": "amount",
            "entity_type": "numeric",
            "co_occurrence_count": 2,
        }
    ]
    html = _render(
        "pages/discovery_entity_detail.html",
        entity_text="user_email",
        entity_type="free_text",
        assets=assets,
        co_occurring=co,
    )
    assert "user_email" in html
    assert "disc-badge-free_text" in html
    assert "/dashboard/discovery/assets/7" in html
    assert "/dashboard/discovery/entities/numeric/amount" in html


# ---------------------------------------------------------------------------
# pages/discovery_category_detail.html
# ---------------------------------------------------------------------------


def test_category_detail_shows_breadcrumb_children_and_assets() -> None:
    html = _render(
        "pages/discovery_category_detail.html",
        category={
            "id": 1,
            "name": "Public",
            "path": "actualize.public",
            "description": None,
            "document_count": 12,
            "created_at": "2026-05-15T00:00:00+00:00",
        },
        children=[
            {
                "id": 2,
                "name": "Auth",
                "path": "actualize.public.auth",
                "doc_count": 4,
                "has_children": False,
            },
        ],
        assets=[
            {
                "id": 11,
                "source_id": "actualize",
                "asset_type": "table",
                "asset_path": "public.auth_user",
                "title": "auth_user",
                "summary": "",
                "updated_at": "2026-05-15T01:00:00+00:00",
            },
        ],
        breadcrumb=[
            {"label": "actualize", "path": "actualize"},
            {"label": "public", "path": "actualize.public"},
        ],
    )
    assert "Public" in html
    # Breadcrumb links: first segment links, last is bold.
    assert "/dashboard/discovery/categories/actualize" in html
    assert "<strong>public</strong>" in html
    # Children -> further category detail.
    assert "/dashboard/discovery/categories/actualize.public.auth" in html
    # Assets -> asset detail.
    assert "/dashboard/discovery/assets/11" in html


# ---------------------------------------------------------------------------
# partials/discovery_rescan_status.html
# ---------------------------------------------------------------------------


def test_rescan_status_reports_queued_and_skipped_sources() -> None:
    html = _render(
        "partials/discovery_rescan_status.html",
        result={"queued": ["sales_pg", "hr_mysql"], "skipped": ["team_slack"]},
        last_run=_overview(pending=2),
    )
    assert "Queued a catalog scan for 2 sources: sales_pg, hr_mysql" in html
    assert "1 skipped" in html and "team_slack" in html
    assert "2 waiting" in html


def test_rescan_status_before_any_scan() -> None:
    html = _render("partials/discovery_rescan_status.html", result=None, last_run=None)
    assert "No catalog scan has finished yet" in html


def test_rescan_status_renders_top_level_error() -> None:
    html = _render(
        "partials/discovery_rescan_status.html",
        result={"error": "PostgresConnectionError"},
        last_run=None,
    )
    assert "Rescan failed" in html
    assert "PostgresConnectionError" in html
