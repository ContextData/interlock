"""PostgreSQL structure from `pg_catalog`.

Connects through `ConnectionManager.get_pool`, the same path agent queries use,
so secret references, `sslmode`/`ssl_ca` and the production refusal of
unverified TLS all apply. The scanner this replaces opened its own pool with a
plain password and no TLS settings, which is why it could not scan a source
configured the way production requires.

Reads `pg_catalog` rather than `information_schema` so it sees views,
materialized views and foreign tables as well as base tables, skips child
partitions (their parent represents them), and records whether the scan's
login can actually read each relation.
"""

from __future__ import annotations

from typing import Any

from interlock.catalog.collectors import CollectContext
from interlock.catalog.model import CatalogNode, CatalogSnapshot, sql_resource_key
from interlock.discovery.cataloger import classify_column

_STATEMENT_TIMEOUT_MS = 60_000

_RELKIND_NODE_TYPE = {
    "r": "table",
    "p": "table",
    "v": "view",
    "m": "materialized_view",
    "f": "foreign_table",
}

_SYSTEM_SCHEMA_FILTER = """
    n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\\_toast%'
    AND n.nspname NOT LIKE 'pg\\_temp%'
"""

_SCHEMAS_SQL = f"""
SELECT n.nspname AS schema_name
FROM pg_namespace n
WHERE {_SYSTEM_SCHEMA_FILTER}
  AND has_schema_privilege(n.oid, 'USAGE')
ORDER BY n.nspname
"""

_RELATIONS_SQL = f"""
SELECT n.nspname AS schema_name,
       c.relname AS relation_name,
       c.relkind::text AS relkind,
       c.reltuples::bigint AS row_estimate,
       has_table_privilege(c.oid, 'SELECT') AS can_select,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS is_nullable,
       a.attnum::int AS ordinal
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attribute a
       ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT c.relispartition
  AND {_SYSTEM_SCHEMA_FILTER}
  AND has_schema_privilege(n.oid, 'USAGE')
ORDER BY n.nspname, c.relname, a.attnum
"""

_SOURCE_FACTS_SQL = """
SELECT current_setting('search_path') AS search_path,
       current_user AS login,
       current_database() AS database,
       current_setting('server_version') AS server_version,
       current_schemas(false) AS effective_schemas
"""


async def collect_postgres(ctx: CollectContext) -> CatalogSnapshot:
    if ctx.conn_manager is None:
        raise RuntimeError("PostgreSQL collection needs the connection manager")
    pool = await ctx.conn_manager.get_pool(ctx.source_id)
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}")
            facts = await conn.fetchrow(_SOURCE_FACTS_SQL)
            schema_rows = await conn.fetch(_SCHEMAS_SQL)
            relation_rows = await conn.fetch(_RELATIONS_SQL)
    return build_postgres_snapshot(facts, schema_rows, relation_rows, ctx.limits)


def _default_schema(effective_schemas: list[str]) -> str:
    return effective_schemas[0] if effective_schemas else "public"


def build_postgres_snapshot(
    facts: Any, schema_rows: list[Any], relation_rows: list[Any], limits: Any
) -> CatalogSnapshot:
    """Pure transformation from catalog rows to a snapshot, so it is testable
    without a database and the caps can be exercised exactly."""
    effective = list(facts["effective_schemas"] or []) if facts else []
    snapshot = CatalogSnapshot(
        source_attributes={
            "dialect": "postgres",
            "search_path": facts["search_path"] if facts else None,
            "effective_schemas": effective,
            "default_schema": _default_schema(effective),
            "login": facts["login"] if facts else None,
            "database": facts["database"] if facts else None,
            "server_version": facts["server_version"] if facts else None,
        }
    )
    nodes = snapshot.nodes
    truncation: dict[str, Any] = {}

    schemas: list[str] = []
    for row in schema_rows:
        if len(schemas) >= limits.max_schemas:
            truncation["max_schemas"] = limits.max_schemas
            break
        schemas.append(row["schema_name"])
    kept_schemas = set(schemas)
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
    incomplete_schemas: set[str] = set()
    current: tuple[str, str] | None = None
    columns_in_current = 0
    skip_current = False
    for row in relation_rows:
        schema, relation = row["schema_name"], row["relation_name"]
        if schema not in kept_schemas:
            continue
        if (schema, relation) != current:
            current = (schema, relation)
            columns_in_current = 0
            if tables_seen >= limits.max_tables or len(nodes) >= limits.max_nodes:
                truncation.setdefault("max_tables", limits.max_tables)
                incomplete_schemas.add(schema)
                skip_current = True
                continue
            skip_current = False
            tables_seen += 1
            estimate = row["row_estimate"]
            nodes.append(
                CatalogNode(
                    node_type=_RELKIND_NODE_TYPE.get(row["relkind"], "table"),
                    path=(schema, relation),
                    name=relation,
                    resource_key=sql_resource_key(schema, relation),
                    attributes={
                        "row_estimate": (
                            int(estimate) if estimate is not None and estimate >= 0 else None
                        ),
                        "can_select": bool(row["can_select"]),
                        "partitioned": row["relkind"] == "p",
                    },
                )
            )
        if skip_current or row["column_name"] is None:
            continue
        if columns_in_current >= limits.max_columns_per_table or len(nodes) >= limits.max_nodes:
            truncation.setdefault("max_columns_per_table", limits.max_columns_per_table)
            incomplete_schemas.add(schema)
            continue
        columns_in_current += 1
        column = row["column_name"]
        data_type = row["data_type"]
        nodes.append(
            CatalogNode(
                node_type="column",
                path=(schema, relation, column),
                name=column,
                resource_key=sql_resource_key(schema, relation, column),
                ordinal=row["ordinal"],
                data_type=data_type,
                heuristic_class=classify_column(column, data_type or ""),
                attributes={"nullable": bool(row["is_nullable"])},
            )
        )

    snapshot.truncated = bool(truncation)
    snapshot.truncation = truncation
    if not truncation:
        snapshot.complete_subtrees = [()]
    elif "max_schemas" not in truncation:
        snapshot.complete_subtrees = [(s,) for s in schemas if s not in incomplete_schemas]
    return snapshot
