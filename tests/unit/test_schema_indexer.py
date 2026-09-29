"""Unit tests for the schema -> discovery catalog indexer.

These exercise the pure logic in SchemaIndexer (text formatters,
category-path derivation, column classification entity mapping) plus
the mapping from source catalog rows to discovery assets. They do NOT need a live
Postgres - integration-level tests live in tests/integration/.
"""

from __future__ import annotations

from interlock.discovery.schema_indexer import (
    _assets_from_catalog_rows,
    _column_entities,
    _derive_category_path,
    _format_search_content,
    _format_summary_text,
)

# ---------------------------------------------------------------------------
# Text formatters
# ---------------------------------------------------------------------------


def test_format_summary_text_short_table() -> None:
    cols = [
        {"column_name": "id", "data_type": "int"},
        {"column_name": "email", "data_type": "text"},
    ]
    out = _format_summary_text("users", cols)
    assert "users" in out
    assert "2 columns" in out
    assert "id, email" in out
    # No ellipsis for short tables.
    assert "..." not in out


def test_format_summary_text_truncates_long_column_lists() -> None:
    cols = [{"column_name": f"c{i}", "data_type": "text"} for i in range(20)]
    out = _format_summary_text("wide", cols)
    assert "20 columns" in out
    assert "..." in out


def test_format_search_content_includes_table_and_column_names_and_types() -> None:
    cols = [
        {"column_name": "user_id", "data_type": "integer"},
        {"column_name": "email_address", "data_type": "text"},
    ]
    out = _format_search_content("user_profile", cols)
    # Original token + space-split variant for fulltext recall.
    assert "user_profile" in out
    assert "user profile" in out
    assert "user_id" in out
    assert "user id" in out
    assert "email_address" in out
    assert "email address" in out
    assert "integer" in out
    assert "text" in out


# ---------------------------------------------------------------------------
# Category path derivation
# ---------------------------------------------------------------------------


def test_derive_category_path_uses_prefix_when_table_name_has_underscore() -> None:
    # "auth_user" -> prefix "auth" becomes a category leaf so all
    # tables that share the prefix cluster together.
    assert _derive_category_path("shop", "public", "auth_user") == "shop.public.auth"


def test_derive_category_path_omits_prefix_when_name_is_single_token() -> None:
    # No underscore in "orders" -> we stop at the schema level. The
    # caller (DocumentIndexer) sanitizes the path further for ltree.
    assert _derive_category_path("shop", "public", "orders") == "shop.public"


# ---------------------------------------------------------------------------
# Column -> entity classification
# ---------------------------------------------------------------------------


def test_column_entities_classifies_by_name_and_type() -> None:
    cols = [
        {"column_name": "id", "data_type": "integer"},
        {"column_name": "description", "data_type": "text"},
        {"column_name": "created_at", "data_type": "timestamp"},
        {"column_name": "is_active", "data_type": "boolean"},
        {"column_name": "price", "data_type": "numeric"},
        {"column_name": "title", "data_type": "varchar"},
    ]
    out = {(e["text"], e["type"]) for e in _column_entities(cols)}
    assert ("id", "identifier") in out
    # "description" hits the FREE_TEXT_PATTERNS list in pii_deep.
    assert ("description", "free_text") in out
    assert ("created_at", "temporal") in out
    assert ("is_active", "boolean") in out
    assert ("price", "numeric") in out
    # "title" is varchar with no identifier suffix -> unknown.
    assert ("title", "unknown") in out


def test_column_entities_skips_blank_names() -> None:
    cols = [
        {"column_name": "", "data_type": "text"},
        {"column_name": "name", "data_type": "text"},
    ]
    out = _column_entities(cols)
    assert len(out) == 1
    assert out[0]["text"] == "name"


# ---------------------------------------------------------------------------
# Catalog rows -> discovery assets
# ---------------------------------------------------------------------------


def _catalog_row(
    table: tuple[str, ...], column: str | None, ordinal: int | None = None
) -> dict[str, object]:
    return {
        "table_path": list(table),
        "table_name": table[-1],
        "table_attributes": '{"row_estimate": 42}',
        "column_name": column,
        "data_type": "text" if column else None,
        "ordinal": ordinal,
        "column_attributes": {"nullable": True} if column else None,
    }


def test_catalog_rows_group_into_one_asset_per_table() -> None:
    rows = [
        _catalog_row(("sales", "customers"), "id", 1),
        _catalog_row(("sales", "customers"), "email", 2),
        _catalog_row(("hr", "empty_table"), None),
    ]
    assets = _assets_from_catalog_rows("src", rows)
    by_path = {a["asset_path"]: a for a in assets}
    assert set(by_path) == {"sales.customers", "hr.empty_table"}
    customers = by_path["sales.customers"]["summary"]
    assert [c["column_name"] for c in customers["columns"]] == ["id", "email"]
    assert customers["column_count"] == 2
    assert customers["row_estimate"] == 42
    assert customers["columns"][1]["is_nullable"] == "YES"
    # A table with no columns is still an asset, with none listed.
    assert by_path["hr.empty_table"]["summary"]["column_count"] == 0


def test_catalog_rows_yield_nothing_for_an_empty_catalog() -> None:
    assert _assets_from_catalog_rows("src", []) == []
