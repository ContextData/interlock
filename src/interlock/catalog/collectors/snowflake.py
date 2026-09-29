"""Snowflake structure from the configured database's `information_schema`.

Connects through `SnowflakeAdapter._connect_sync`, so `password_ref`,
`private_key_ref`, the role and the warehouse are applied exactly as for agent
queries. Every schema of the configured database is covered; other databases
are not, because governance refuses references into them.

Scheduled scans cost warehouse credits, which is why the scan interval can be
set per source, down to 0 to switch scheduled refresh off.

Paths omit the database, which is recorded on the source node: the catalog
holds one database, and resource keys stay in the two-part `schema.table` form
enforcement matches today.
"""

from __future__ import annotations

import asyncio
from typing import Any

from interlock.catalog.collectors import CollectContext
from interlock.catalog.collectors.mysql import build_mysql_snapshot
from interlock.catalog.errors import CatalogScanError
from interlock.catalog.model import CatalogSnapshot


def _collect_sync(ctx: CollectContext) -> dict[str, Any]:
    from interlock.connections.connectors import SnowflakeAdapter, get_adapter

    adapter = get_adapter(ctx.connector_key, {"connector_key": ctx.connector_key})
    if not isinstance(adapter, SnowflakeAdapter):
        raise CatalogScanError(
            "unsupported", f"{ctx.connector_key} does not resolve to the Snowflake adapter"
        )
    database = str(ctx.connection_config.get("database") or "")
    if not database:
        raise ValueError("Snowflake catalog collection needs a configured database")
    quoted = '"' + database.replace('"', '""') + '"'
    conn = adapter._connect_sync(ctx.connection_config)
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT CURRENT_USER(), CURRENT_VERSION(), CURRENT_SCHEMA()")
            login, version, current_schema = cursor.fetchone()
            cursor.execute(
                f"SELECT schema_name FROM {quoted}.information_schema.schemata "
                "WHERE schema_name <> 'INFORMATION_SCHEMA' ORDER BY schema_name"
            )
            schema_rows = [row[0] for row in cursor.fetchall()]
            cursor.execute(f"""
                SELECT t.table_schema, t.table_name, t.table_type, t.row_count,
                       c.column_name, c.data_type, c.is_nullable, c.ordinal_position
                FROM {quoted}.information_schema.tables t
                LEFT JOIN {quoted}.information_schema.columns c
                       ON c.table_schema = t.table_schema AND c.table_name = t.table_name
                WHERE t.table_schema <> 'INFORMATION_SCHEMA'
                ORDER BY t.table_schema, t.table_name, c.ordinal_position
                """)
            relation_rows = [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
    finally:
        conn.close()
    return {
        "login": login,
        "server_version": version,
        "database": database,
        "default_schema": str(ctx.connection_config.get("schema") or current_schema or "") or None,
        "schema_rows": schema_rows,
        "relation_rows": relation_rows,
    }


async def collect_snowflake(ctx: CollectContext) -> CatalogSnapshot:
    raw = await asyncio.to_thread(_collect_sync, ctx)
    snapshot = build_mysql_snapshot(
        facts={
            "login": raw["login"],
            "server_version": raw["server_version"],
            "database": raw["database"],
            "default_schema": raw["default_schema"],
        },
        schema_rows=raw["schema_rows"],
        relation_rows=raw["relation_rows"],
        limits=ctx.limits,
    )
    snapshot.source_attributes["dialect"] = "snowflake"
    return snapshot
