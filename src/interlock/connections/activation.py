"""Which connectors an operator may register new sources on.

Activation governs registration only: a source already registered on a
connector that is later deactivated keeps serving traffic. `planned` connectors
have no real adapter and can never be activated. The seed set lives in
migrations/018_connector_activation.sql.
"""

from __future__ import annotations

from typing import Any

from interlock.connections.connectors import CONNECTOR_DEFINITIONS, ConnectorDefinition

# Shown in this order in the connector dropdowns and on the Connectors page.
CATEGORY_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Databases and warehouses", ("database", "warehouse")),
    ("Object storage", ("object_storage",)),
    ("Collaboration", ("collaboration",)),
    ("Code repositories", ("repo",)),
    ("Business apps", ("saas", "workspace")),
    ("Search and vector", ("search",)),
    ("Web APIs", ("http",)),
)


# "Stable V1" in docs-site/src/content/docs/reference/connector-support-matrix.md; everything else is beta.
STABLE_CONNECTOR_KEYS = frozenset(
    {"postgresql", "mysql", "mariadb", "generic_rest", "s3", "digitalocean_spaces"}
)


# Active on a fresh deployment: the connectors that have been run against real
# systems. Migration 018 seeds exactly these; a test holds the two together.
DEFAULT_ACTIVE_CONNECTOR_KEYS = frozenset(
    {"postgresql", "mysql", "s3", "slack", "github", "generic_rest"}
)


class ConnectorNotActiveError(ValueError):
    """A source was registered on a connector that is not active."""


def activatable(connector_key: str) -> bool:
    definition = CONNECTOR_DEFINITIONS.get(connector_key)
    return definition is not None and definition.status != "planned"


def category_of(definition: ConnectorDefinition) -> str:
    for label, families in CATEGORY_ORDER:
        if definition.family in families:
            return label
    return "Other"


def group_by_category(
    definitions: list[ConnectorDefinition],
) -> list[tuple[str, list[ConnectorDefinition]]]:
    grouped: dict[str, list[ConnectorDefinition]] = {}
    for definition in definitions:
        grouped.setdefault(category_of(definition), []).append(definition)
    order = [label for label, _ in CATEGORY_ORDER] + ["Other"]
    return [
        (label, sorted(grouped[label], key=lambda d: d.name)) for label in order if label in grouped
    ]


async def active_connector_keys(pool: Any) -> frozenset[str]:
    rows = await pool.fetch("SELECT connector_key FROM connector_activation WHERE active")
    return frozenset(str(r["connector_key"]) for r in rows if activatable(str(r["connector_key"])))


async def active_connector_groups(pool: Any) -> list[tuple[str, list[ConnectorDefinition]]]:
    """Active connectors grouped for a dropdown."""
    keys = await active_connector_keys(pool)
    return group_by_category([CONNECTOR_DEFINITIONS[key] for key in sorted(keys)])


async def require_active(pool: Any, connector_key: str) -> ConnectorDefinition:
    """The connector's definition, or ConnectorNotActiveError."""
    definition = CONNECTOR_DEFINITIONS.get(connector_key)
    if definition is None:
        raise ConnectorNotActiveError(f"Unknown connector: {connector_key!r}.")
    if connector_key not in await active_connector_keys(pool):
        raise ConnectorNotActiveError(
            f"The {definition.name} connector is not active. "
            "An administrator can activate it on the Connectors page."
        )
    return definition


async def set_active(pool: Any, connector_key: str, active: bool, admin_id: int | None) -> None:
    if not activatable(connector_key):
        raise ConnectorNotActiveError(f"The connector {connector_key!r} cannot be activated.")
    await pool.execute(
        """
        INSERT INTO connector_activation (connector_key, active, updated_at, updated_by)
        VALUES ($1, $2, NOW(), $3)
        ON CONFLICT (connector_key) DO UPDATE
        SET active = EXCLUDED.active, updated_at = NOW(), updated_by = EXCLUDED.updated_by
        """,
        connector_key,
        active,
        admin_id,
    )


async def connector_rows(pool: Any) -> list[dict[str, Any]]:
    """Every activatable connector with its status and how many sources use it."""
    from interlock.connections.connectors import connector_key_for

    active = await active_connector_keys(pool)
    in_use: dict[str, int] = {}
    for row in await pool.fetch("SELECT source_type, metadata FROM data_sources"):
        metadata = row["metadata"]
        key = connector_key_for(
            str(row["source_type"]), metadata if isinstance(metadata, dict) else None
        )
        in_use[key] = in_use.get(key, 0) + 1
    rows: list[dict[str, Any]] = []
    for definition in CONNECTOR_DEFINITIONS.values():
        if definition.status == "planned":
            continue
        rows.append(
            {
                "key": definition.key,
                "name": definition.name,
                "description": definition.description,
                "category": category_of(definition),
                "maturity": "Stable" if definition.key in STABLE_CONNECTOR_KEYS else "Beta",
                "active": definition.key in active,
                "sources": in_use.get(definition.key, 0),
            }
        )
    order = {label: i for i, (label, _) in enumerate(CATEGORY_ORDER)}
    rows.sort(key=lambda r: (order.get(r["category"], len(order)), r["name"]))
    return rows
