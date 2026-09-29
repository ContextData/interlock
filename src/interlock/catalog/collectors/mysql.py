"""MySQL and MariaDB structure from `information_schema`.

Connects through `MySQLAdapter._connect`, the helper agent queries use, so the
host guard, `password_ref` and `ssl`/`ssl_ca` settings apply unchanged. The
adapter's own `introspect()` covered only the configured database; this covers
every schema the login can see, because a role may reference any of them.
"""

from __future__ import annotations

from typing import Any

from interlock.catalog.collectors import CollectContext
from interlock.catalog.errors import CatalogScanError
from interlock.catalog.model import CatalogNode, CatalogSnapshot, ScanLimits, sql_resource_key
from interlock.discovery.cataloger import classify_column

_SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")

_SCHEMAS_SQL = """
SELECT SCHEMA_NAME FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME NOT IN (%s, %s, %s, %s)
ORDER BY SCHEMA_NAME
"""

_RELATIONS_SQL = """
SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE, t.TABLE_ROWS,
       c.COLUMN_NAME, c.COLUMN_TYPE, c.IS_NULLABLE, c.ORDINAL_POSITION
FROM information_schema.TABLES t
LEFT JOIN information_schema.COLUMNS c
       ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
WHERE t.TABLE_SCHEMA NOT IN (%s, %s, %s, %s)
ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION
"""


async def collect_mysql(ctx: CollectContext) -> CatalogSnapshot:
    from interlock.connections.connectors import MySQLAdapter, get_adapter

    adapter = get_adapter(ctx.connector_key, {"connector_key": ctx.connector_key})
    if not isinstance(adapter, MySQLAdapter):
        raise CatalogScanError(
            "unsupported", f"{ctx.connector_key} does not resolve to the MySQL adapter"
        )
    conn = await adapter._connect(ctx.connection_config)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SET SESSION TRANSACTION READ ONLY")
            await cursor.execute("SELECT CURRENT_USER(), VERSION(), DATABASE()")
            login, version, current_db = await cursor.fetchone()
            await cursor.execute(_SCHEMAS_SQL, _SYSTEM_SCHEMAS)
            schema_rows = await cursor.fetchall()
            await cursor.execute(_RELATIONS_SQL, _SYSTEM_SCHEMAS)
            relation_rows = await cursor.fetchall()
    finally:
        conn.close()

    default_schema = str(ctx.connection_config.get("database") or current_db or "")
    return build_mysql_snapshot(
        facts={
            "login": login,
            "server_version": version,
            "default_schema": default_schema or None,
        },
        schema_rows=[row[0] for row in schema_rows],
        relation_rows=[tuple(row) for row in relation_rows],
        limits=ctx.limits,
    )


def build_mysql_snapshot(
    *,
    facts: dict[str, Any],
    schema_rows: list[str],
    relation_rows: list[tuple[Any, ...]],
    limits: ScanLimits,
) -> CatalogSnapshot:
    snapshot = CatalogSnapshot(source_attributes={"dialect": "mysql", **facts})
    nodes = snapshot.nodes
    truncation: dict[str, Any] = {}

    schemas = list(schema_rows[: limits.max_schemas])
    if len(schema_rows) > limits.max_schemas:
        truncation["max_schemas"] = limits.max_schemas
    kept = set(schemas)
    for schema in schemas:
        nodes.append(
            CatalogNode(
                node_type="schema",
                path=(schema,),
                name=schema,
                resource_key=sql_resource_key(schema),
            )
        )

    tables_seen = 0
    incomplete: set[str] = set()
    current: tuple[str, str] | None = None
    columns_in_current = 0
    skip_current = False
    for (
        schema,
        table,
        table_type,
        table_rows,
        column,
        column_type,
        nullable,
        ordinal,
    ) in relation_rows:
        if schema not in kept:
            continue
        if (schema, table) != current:
            current = (schema, table)
            columns_in_current = 0
            if tables_seen >= limits.max_tables or len(nodes) >= limits.max_nodes:
                truncation.setdefault("max_tables", limits.max_tables)
                incomplete.add(schema)
                skip_current = True
                continue
            skip_current = False
            tables_seen += 1
            nodes.append(
                CatalogNode(
                    node_type="view" if str(table_type).upper() == "VIEW" else "table",
                    path=(schema, table),
                    name=table,
                    resource_key=sql_resource_key(schema, table),
                    attributes={
                        "row_estimate": int(table_rows) if table_rows is not None else None
                    },
                )
            )
        if skip_current or column is None:
            continue
        if columns_in_current >= limits.max_columns_per_table or len(nodes) >= limits.max_nodes:
            truncation.setdefault("max_columns_per_table", limits.max_columns_per_table)
            incomplete.add(schema)
            continue
        columns_in_current += 1
        nodes.append(
            CatalogNode(
                node_type="column",
                path=(schema, table, column),
                name=column,
                resource_key=sql_resource_key(schema, table, column),
                ordinal=int(ordinal) if ordinal is not None else None,
                data_type=str(column_type) if column_type is not None else None,
                heuristic_class=classify_column(column, str(column_type or "")),
                attributes={"nullable": str(nullable).upper() == "YES"},
            )
        )

    snapshot.truncated = bool(truncation)
    snapshot.truncation = truncation
    if not truncation:
        snapshot.complete_subtrees = [()]
    elif "max_schemas" not in truncation:
        snapshot.complete_subtrees = [(s,) for s in schemas if s not in incomplete]
    return snapshot
