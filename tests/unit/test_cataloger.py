"""Tests for SchemaCataloger - DB schema introspection and cataloging."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.discovery.cataloger import SchemaCataloger, classify_column

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool() -> AsyncMock:
    """Create a mock asyncpg.Pool with async methods."""
    return AsyncMock()


def _make_row(data: dict) -> MagicMock:
    """Create a mock asyncpg.Record."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.keys = lambda: data.keys()
    row.values = lambda: data.values()
    row.items = lambda: data.items()
    row.__iter__ = lambda self: iter(data)
    row.__len__ = lambda self: len(data)
    return row


# ---------------------------------------------------------------------------
# classify_column
# ---------------------------------------------------------------------------


class TestClassifyColumn:
    @pytest.mark.parametrize(
        "col_name, data_type, expected",
        [
            ("description", "text", "free_text"),
            ("user_notes", "text", "free_text"),
            ("comment_body", "text", "free_text"),
            ("bio", "varchar", "free_text"),
            ("message", "text", "free_text"),
            ("body", "text", "free_text"),
            ("content", "text", "free_text"),
            ("remarks", "text", "free_text"),
            ("summary", "text", "free_text"),
            ("feedback", "text", "free_text"),
            ("review", "text", "free_text"),
        ],
    )
    def test_free_text_columns(self, col_name: str, data_type: str, expected: str) -> None:
        assert classify_column(col_name, data_type) == expected

    @pytest.mark.parametrize(
        "col_name, data_type, expected",
        [
            ("id", "bigint", "identifier"),
            ("user_id", "integer", "identifier"),
            ("pk", "integer", "identifier"),
            ("account_pk", "bigint", "identifier"),
            ("uuid", "uuid", "identifier"),
            ("session_uuid", "uuid", "identifier"),
            ("api_key", "text", "identifier"),
            ("ref_code", "text", "identifier"),
        ],
    )
    def test_identifier_columns(self, col_name: str, data_type: str, expected: str) -> None:
        assert classify_column(col_name, data_type) == expected

    @pytest.mark.parametrize(
        "col_name, data_type, expected",
        [
            ("age", "integer", "numeric"),
            ("price", "numeric", "numeric"),
            ("score", "double precision", "numeric"),
            ("amount", "decimal", "numeric"),
            ("count", "bigint", "numeric"),
        ],
    )
    def test_numeric_columns(self, col_name: str, data_type: str, expected: str) -> None:
        assert classify_column(col_name, data_type) == expected

    @pytest.mark.parametrize(
        "col_name, data_type, expected",
        [
            ("created_at", "timestamptz", "temporal"),
            ("updated_at", "timestamp with time zone", "temporal"),
            ("birthday", "date", "temporal"),
            ("login_time", "time", "temporal"),
        ],
    )
    def test_temporal_columns(self, col_name: str, data_type: str, expected: str) -> None:
        assert classify_column(col_name, data_type) == expected

    @pytest.mark.parametrize(
        "col_name, data_type, expected",
        [
            ("is_active", "boolean", "boolean"),
            ("enabled", "bool", "boolean"),
        ],
    )
    def test_boolean_columns(self, col_name: str, data_type: str, expected: str) -> None:
        assert classify_column(col_name, data_type) == expected

    def test_unknown_column(self) -> None:
        assert classify_column("data", "jsonb") == "unknown"


# ---------------------------------------------------------------------------
# Free-text detection (integration with pii_deep patterns)
# ---------------------------------------------------------------------------


class TestFreeTextDetection:
    """Verify that column names are correctly detected as free-text."""

    @pytest.mark.parametrize(
        "col_name",
        [
            "description",
            "notes",
            "comment",
            "bio",
            "message",
            "body",
            "content",
            "remarks",
            "summary",
            "text",
            "detail",
            "narrative",
            "memo",
            "review",
            "feedback",
            "user_description",
            "customer_feedback",
            "order_notes",
        ],
    )
    def test_free_text_names_detected(self, col_name: str) -> None:
        result = classify_column(col_name, "text")
        assert result == "free_text", f"{col_name} should be classified as free_text"

    @pytest.mark.parametrize(
        "col_name",
        [
            "email",
            "phone",
            "name",
            "status",
            "address",
            "title",
            "role",
        ],
    )
    def test_non_free_text_names(self, col_name: str) -> None:
        # These should NOT be classified as free_text (they may be identifier,
        # or unknown depending on data_type)
        result = classify_column(col_name, "text")
        assert result != "free_text", f"{col_name} should not be classified as free_text"


# ---------------------------------------------------------------------------
# introspect_source
# ---------------------------------------------------------------------------


class TestIntrospectSource:
    @pytest.mark.asyncio
    async def test_introspect_returns_correct_structure(self) -> None:
        onyx_pool = _make_pool()
        source_pool = _make_pool()

        # Mock information_schema query result
        info_rows = [
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "id",
                    "data_type": "bigint",
                    "is_nullable": "NO",
                    "column_default": "nextval('users_id_seq'::regclass)",
                    "ordinal_position": 1,
                }
            ),
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "name",
                    "data_type": "text",
                    "is_nullable": "NO",
                    "column_default": None,
                    "ordinal_position": 2,
                }
            ),
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "bio",
                    "data_type": "text",
                    "is_nullable": "YES",
                    "column_default": None,
                    "ordinal_position": 3,
                }
            ),
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "orders",
                    "column_name": "id",
                    "data_type": "bigint",
                    "is_nullable": "NO",
                    "column_default": None,
                    "ordinal_position": 1,
                }
            ),
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "orders",
                    "column_name": "total",
                    "data_type": "numeric",
                    "is_nullable": "YES",
                    "column_default": None,
                    "ordinal_position": 2,
                }
            ),
        ]

        # Mock pg_stat_user_tables
        stat_rows = [
            _make_row({"schemaname": "public", "relname": "users", "n_live_tup": 1500}),
            _make_row({"schemaname": "public", "relname": "orders", "n_live_tup": 50000}),
        ]

        source_pool.fetch.side_effect = [info_rows, stat_rows]

        cataloger = SchemaCataloger(onyx_pool)
        assets = await cataloger.introspect_source("test_source", source_pool)

        assert len(assets) == 2

        # Check users table asset
        users_asset = next(a for a in assets if a["title"] == "users")
        assert users_asset["source_id"] == "test_source"
        assert users_asset["asset_type"] == "table"
        assert users_asset["asset_path"] == "public.users"
        assert users_asset["summary"]["column_count"] == 3
        assert users_asset["summary"]["row_estimate"] == 1500
        assert "users" in users_asset["topics"]

        # Check orders table asset
        orders_asset = next(a for a in assets if a["title"] == "orders")
        assert orders_asset["asset_path"] == "public.orders"
        assert orders_asset["summary"]["row_estimate"] == 50000
        assert orders_asset["summary"]["column_count"] == 2

    @pytest.mark.asyncio
    async def test_introspect_empty_source(self) -> None:
        onyx_pool = _make_pool()
        source_pool = _make_pool()
        source_pool.fetch.side_effect = [[], []]

        cataloger = SchemaCataloger(onyx_pool)
        assets = await cataloger.introspect_source("empty_source", source_pool)

        assert assets == []


# ---------------------------------------------------------------------------
# update_schema_catalog
# ---------------------------------------------------------------------------


class TestUpdateSchemaCatalog:
    @pytest.mark.asyncio
    async def test_classifies_columns_correctly(self) -> None:
        onyx_pool = _make_pool()
        source_pool = _make_pool()

        column_rows = [
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "id",
                    "data_type": "bigint",
                }
            ),
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "bio",
                    "data_type": "text",
                }
            ),
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "created_at",
                    "data_type": "timestamptz",
                }
            ),
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "is_active",
                    "data_type": "boolean",
                }
            ),
            _make_row(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "score",
                    "data_type": "double precision",
                }
            ),
        ]

        source_pool.fetch.return_value = column_rows

        cataloger = SchemaCataloger(onyx_pool)
        count = await cataloger.update_schema_catalog("test_source", source_pool)

        assert count == 5
        assert onyx_pool.execute.call_count == 5

        # Verify classifications by inspecting the execute calls
        calls = onyx_pool.execute.call_args_list

        # id -> identifier, pii_scan_tier -> fast_only
        assert calls[0][0][6] == "identifier"
        assert calls[0][0][7] == "fast_only"

        # bio -> free_text, pii_scan_tier -> deep
        assert calls[1][0][6] == "free_text"
        assert calls[1][0][7] == "deep"

        # created_at -> temporal, pii_scan_tier -> skip
        assert calls[2][0][6] == "temporal"
        assert calls[2][0][7] == "skip"

        # is_active -> boolean, pii_scan_tier -> skip
        assert calls[3][0][6] == "boolean"
        assert calls[3][0][7] == "skip"

        # score -> numeric, pii_scan_tier -> skip
        assert calls[4][0][6] == "numeric"
        assert calls[4][0][7] == "skip"
