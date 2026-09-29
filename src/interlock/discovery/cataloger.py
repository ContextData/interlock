"""DB schema introspection and cataloging for PostgreSQL data sources."""

from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg

from interlock.pipeline.pii_deep import is_free_text_column

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column classification heuristics
# ---------------------------------------------------------------------------

_IDENTIFIER_PATTERNS = re.compile(
    r"(^id$|_id$|^pk$|_pk$|^uuid$|_uuid$|^key$|_key$|^code$|_code$)",
    re.IGNORECASE,
)

_TEMPORAL_TYPES = frozenset(
    {
        "timestamp",
        "timestamptz",
        "timestamp with time zone",
        "timestamp without time zone",
        "date",
        "time",
        "time with time zone",
        "time without time zone",
        "interval",
    }
)

_BOOLEAN_TYPES = frozenset({"boolean", "bool"})

_NUMERIC_TYPES = frozenset(
    {
        "smallint",
        "integer",
        "bigint",
        "real",
        "double precision",
        "numeric",
        "decimal",
        "float4",
        "float8",
        "int2",
        "int4",
        "int8",
        "serial",
        "bigserial",
        "smallserial",
        "money",
    }
)


def classify_column(column_name: str, data_type: str) -> str:
    """Auto-classify a column based on its name and data type.

    Returns one of: free_text, identifier, numeric, temporal, boolean, unknown.
    """
    if is_free_text_column(column_name):
        return "free_text"
    if _IDENTIFIER_PATTERNS.search(column_name):
        return "identifier"

    dt_lower = data_type.lower()
    if dt_lower in _BOOLEAN_TYPES:
        return "boolean"
    if dt_lower in _TEMPORAL_TYPES:
        return "temporal"
    if dt_lower in _NUMERIC_TYPES:
        return "numeric"

    return "unknown"


# ---------------------------------------------------------------------------
# SchemaCataloger
# ---------------------------------------------------------------------------


class SchemaCataloger:
    """Query information_schema for PostgreSQL sources and catalog as discovery assets."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self._pool = pg_pool

    async def introspect_source(
        self, source_id: str, connection_pool: asyncpg.Pool
    ) -> list[dict[str, Any]]:
        """Query information_schema on a data source to catalog tables/columns.

        Returns a list of asset dicts suitable for inserting into discovery_assets.
        """
        # Fetch all user tables and their columns
        rows = await connection_pool.fetch("""
            SELECT
                t.table_schema,
                t.table_name,
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default,
                c.ordinal_position
            FROM information_schema.tables t
            JOIN information_schema.columns c
                ON c.table_schema = t.table_schema
                AND c.table_name = t.table_name
            WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
                AND t.table_type = 'BASE TABLE'
            ORDER BY t.table_schema, t.table_name, c.ordinal_position
            """)

        # Fetch row estimates from pg_stat_user_tables
        stat_rows = await connection_pool.fetch("""
            SELECT schemaname, relname, n_live_tup
            FROM pg_stat_user_tables
            """)
        row_estimates: dict[str, int] = {}
        for sr in stat_rows:
            key = f"{sr['schemaname']}.{sr['relname']}"
            row_estimates[key] = int(sr["n_live_tup"])

        # Group columns by table
        tables: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            table_key = f"{row['table_schema']}.{row['table_name']}"
            if table_key not in tables:
                tables[table_key] = []
            tables[table_key].append(
                {
                    "column_name": row["column_name"],
                    "data_type": row["data_type"],
                    "is_nullable": row["is_nullable"],
                    "column_default": row["column_default"],
                    "ordinal_position": row["ordinal_position"],
                }
            )

        # Build asset dicts
        assets: list[dict[str, Any]] = []
        for table_path, columns in tables.items():
            table_name = table_path.split(".")[-1]
            # Derive topics from table name (split on underscores)
            topics = [t for t in table_name.split("_") if len(t) > 1]

            assets.append(
                {
                    "source_id": source_id,
                    "asset_type": "table",
                    "asset_path": table_path,
                    "title": table_name,
                    "summary": {
                        "columns": columns,
                        "row_estimate": row_estimates.get(table_path, 0),
                        "column_count": len(columns),
                    },
                    "topics": topics,
                }
            )

        return assets

    async def catalog_to_discovery_assets(
        self, source_id: str, connection_pool: asyncpg.Pool
    ) -> int:
        """Introspect and write results to discovery_assets table.

        Returns count of assets cataloged.
        """
        assets = await self.introspect_source(source_id, connection_pool)

        count = 0
        for asset in assets:
            await self._pool.execute(
                """
                INSERT INTO discovery_assets
                    (source_id, asset_type, asset_path, title, summary, topics)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                ON CONFLICT (source_id, asset_type, asset_path)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    topics = EXCLUDED.topics,
                    updated_at = NOW()
                """,
                asset["source_id"],
                asset["asset_type"],
                asset["asset_path"],
                asset["title"],
                asset["summary"],
                asset["topics"],
            )
            count += 1

        logger.info("Cataloged %d discovery assets for source %s", count, source_id)
        return count

    async def update_schema_catalog(self, source_id: str, connection_pool: asyncpg.Pool) -> int:
        """Update the schema_catalog table with column-level metadata.

        Also auto-classifies columns (free_text, identifier, etc.) based on
        type and name patterns from pii_deep.py.
        Returns count of columns cataloged.
        """
        rows = await connection_pool.fetch("""
            SELECT
                c.table_schema,
                c.table_name,
                c.column_name,
                c.data_type
            FROM information_schema.columns c
            JOIN information_schema.tables t
                ON c.table_schema = t.table_schema
                AND c.table_name = t.table_name
            WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
                AND t.table_type = 'BASE TABLE'
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """)

        count = 0
        for row in rows:
            classification = classify_column(row["column_name"], row["data_type"])
            # Determine PII scan tier based on classification
            if classification == "free_text":
                pii_scan_tier = "deep"
            elif classification == "identifier":
                pii_scan_tier = "fast_only"
            elif classification in ("numeric", "temporal", "boolean"):
                pii_scan_tier = "skip"
            else:
                pii_scan_tier = "auto"

            await self._pool.execute(
                """
                INSERT INTO schema_catalog
                    (source_id, table_schema, table_name, column_name,
                     data_type, classification, pii_scan_tier)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (source_id, table_schema, table_name, column_name)
                DO UPDATE SET
                    data_type = EXCLUDED.data_type,
                    classification = EXCLUDED.classification,
                    pii_scan_tier = EXCLUDED.pii_scan_tier
                """,
                source_id,
                row["table_schema"],
                row["table_name"],
                row["column_name"],
                row["data_type"],
                classification,
                pii_scan_tier,
            )
            count += 1

        logger.info(
            "Cataloged %d columns to schema_catalog for source %s",
            count,
            source_id,
        )
        return count
