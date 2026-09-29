"""Access analytics: the audit trail read against the catalog.

Every governed SQL request records the resources it asked for in
`audit_log.request_metadata.permission_request.resources` - since column-level
enforcement, down to `schema.table.column`. Joined with the catalog that says
which tables are most read, which PII columns agents actually touch, which
catalogued tables nobody has used, and where refusals cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from interlock.catalog.model import TABLE_LIKE_NODE_TYPES, looks_like_pii

_RESOURCES_SQL = """
SELECT r.resource, a.status, COUNT(*) AS requests,
       COUNT(DISTINCT a.identity_id) AS identities
FROM audit_log a,
     jsonb_array_elements_text(
         CASE WHEN jsonb_typeof(a.request_metadata->'permission_request'->'resources') = 'array'
              THEN a.request_metadata->'permission_request'->'resources'
              ELSE '[]'::jsonb END
     ) AS r(resource)
WHERE a.source_id = $1
  AND a.created_at > NOW() - make_interval(days => $2)
  AND a.request_metadata ? 'permission_request'
GROUP BY r.resource, a.status
"""

_CATALOG_SQL = """
SELECT resource_key, node_type FROM source_catalog
WHERE source_id = $1 AND removed_at IS NULL
  AND (node_type = ANY($2::text[]) OR node_type = 'column')
"""

_ANNOTATED_SQL = """
SELECT array_to_string(path, '.') AS key, classification
FROM source_catalog_annotations WHERE source_id = $1
"""


@dataclass(slots=True)
class TableAccess:
    table: str
    requests: int = 0
    denied: int = 0
    identities: int = 0


@dataclass(slots=True)
class AccessAnalytics:
    source_id: str
    days: int
    most_accessed: list[TableAccess] = field(default_factory=list)
    pii_columns: list[tuple[str, int, str]] = field(default_factory=list)
    never_accessed: list[str] = field(default_factory=list)
    denied: list[TableAccess] = field(default_factory=list)
    requests: int = 0


def _table_of(resource: str) -> str | None:
    parts = resource.split(".")
    if len(parts) < 3 or "*" in parts[:2]:
        return None
    return ".".join(parts[:2])


def summarise(
    *,
    source_id: str,
    days: int,
    resource_rows: list[Any],
    catalog_rows: list[Any],
    annotations: dict[str, str],
    limit: int = 25,
) -> AccessAnalytics:
    """Pure: audit resource counts plus the catalog, into the page's four lists."""
    tables = {
        str(row["resource_key"])
        for row in catalog_rows
        if row["node_type"] in TABLE_LIKE_NODE_TYPES
    }
    columns = {str(row["resource_key"]) for row in catalog_rows if row["node_type"] == "column"}
    per_table: dict[str, TableAccess] = {}
    per_column: dict[str, int] = {}
    total = 0
    for row in resource_rows:
        resource = str(row["resource"]).lower()
        requests = int(row["requests"])
        total += requests
        table = _table_of(resource)
        if table is None:
            continue
        entry = per_table.setdefault(table, TableAccess(table=table))
        entry.requests += requests
        entry.identities = max(entry.identities, int(row["identities"]))
        if str(row["status"]) == "denied":
            entry.denied += requests
        elif resource in columns and str(row["status"]) == "success":
            per_column[resource] = per_column.get(resource, 0) + requests

    pii: list[tuple[str, int, str]] = []
    for column, requests in per_column.items():
        # Keys are lower-cased; annotation paths keep their case.
        classification = annotations.get(column)
        if classification in ("pii", "sensitive"):
            pii.append((column, requests, classification))
        elif classification is None and looks_like_pii(column.rsplit(".", 1)[-1]):
            pii.append((column, requests, "likely"))

    return AccessAnalytics(
        source_id=source_id,
        days=days,
        most_accessed=sorted(
            (t for t in per_table.values() if t.requests - t.denied > 0),
            key=lambda t: (-(t.requests - t.denied), t.table),
        )[:limit],
        pii_columns=sorted(pii, key=lambda item: (-item[1], item[0]))[:limit],
        never_accessed=sorted(tables - set(per_table))[:limit],
        denied=sorted(
            (t for t in per_table.values() if t.denied),
            key=lambda t: (-t.denied, t.table),
        )[:limit],
        requests=total,
    )


async def access_analytics(pool: Any, source_id: str, *, days: int = 30) -> AccessAnalytics:
    resource_rows = await pool.fetch(_RESOURCES_SQL, source_id, days)
    catalog_rows = await pool.fetch(_CATALOG_SQL, source_id, sorted(TABLE_LIKE_NODE_TYPES))
    annotations = {
        str(row["key"]).lower(): str(row["classification"])
        for row in await pool.fetch(_ANNOTATED_SQL, source_id)
    }
    return summarise(
        source_id=source_id,
        days=days,
        resource_rows=list(resource_rows),
        catalog_rows=list(catalog_rows),
        annotations=annotations,
    )
