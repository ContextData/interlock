"""What an agent is told it may reach, from the catalog - and nothing more.

`interlock_describe_access` lists, per source, the tables and columns the
caller's roles allow and do not deny. `interlock_describe_source` returns the
catalogued shape of exactly the tables the caller may describe, where it used
to refuse any caller whose role named tables.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.e2e.support.clients import mcp_call
from tests.e2e.test_sql_resource_bypass_characterization import (
    _SEED_TAG,
    _allow_policy,
    _cleanup,
    _identity,
    _notify_policy,
    _role_with,
)

pytestmark = [pytest.mark.e2e]


@pytest.fixture(autouse=True)
async def _catalogued(catalogued_sql_sources: None) -> None:
    """These tests describe the catalog, so the sources must have one."""


def _payload(response: Any) -> Any:
    assert response.status_code == 200, response.text
    body = response.json()
    return json.loads(body["content"][0]["text"])


@pytest.fixture
async def customers_only_key(control_db: Any, e2e_config: Any) -> Any:
    """May read and describe `customers`, but never `customers.ssn`."""
    await _cleanup(control_db)
    role_id = await _role_with(
        control_db,
        source_id=e2e_config.source_id_pg,
        role_key="describe_customers_only",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "public.customers.*",
            },
            {
                "effect": "allow",
                "action": "db.table.describe",
                "resource_type": "db.table",
                "resource_pattern": "public.customers.*",
            },
            {
                "effect": "deny",
                "action": "db.table.select",
                "resource_type": "db.column",
                "resource_pattern": "public.customers.ssn",
            },
        ],
    )
    api_key = await _identity(control_db, source_id=e2e_config.source_id_pg, role_id=role_id)
    await _allow_policy(control_db, e2e_config.source_id_pg)
    await _notify_policy(control_db)
    try:
        yield api_key
    finally:
        await _cleanup(control_db)


async def test_describe_access_lists_only_what_the_roles_reach(
    customers_only_key: str, e2e_config: Any
) -> None:
    summary = _payload(
        mcp_call(
            e2e_config,
            "interlock_describe_access",
            {"source_id": e2e_config.source_id_pg},
            headers={"Authorization": f"Bearer {customers_only_key}"},
        )
    )
    resources = summary[e2e_config.source_id_pg]["resources"]
    assert resources["enforced"] is True and resources["catalogued"] is True
    tables = {entry["table"]: entry for entry in resources["tables"]}
    assert set(tables) == {"public.customers"}
    assert "ssn" not in tables["public.customers"]["columns"]
    assert "name" in tables["public.customers"]["columns"]
    assert tables["public.customers"]["all_columns"] is False
    # Nothing it cannot reach is named, not even to say it is out of reach.
    assert "orders" not in json.dumps(resources)


async def test_describe_source_returns_just_the_describable_tables(
    customers_only_key: str, e2e_config: Any
) -> None:
    rows = _payload(
        mcp_call(
            e2e_config,
            "interlock_describe_source",
            {"source_id": e2e_config.source_id_pg},
            headers={"Authorization": f"Bearer {customers_only_key}"},
        )
    )
    assert rows, rows
    assert {(row["table_schema"], row["table_name"]) for row in rows} == {("public", "customers")}
    assert {"id", "name", "email"} <= {row["column_name"] for row in rows}


async def test_a_deny_policy_is_marked_on_the_table_it_refuses(
    control_db: Any, e2e_config: Any
) -> None:
    await _cleanup(control_db)
    role_id = await _role_with(
        control_db,
        source_id=e2e_config.source_id_pg,
        role_key="describe_everything",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "*.*",
            }
        ],
    )
    api_key = await _identity(control_db, source_id=e2e_config.source_id_pg, role_id=role_id)
    await _allow_policy(control_db, e2e_config.source_id_pg)
    await control_db.execute(
        """
        INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
        VALUES ($1, 50, $2::jsonb, '{"effect": "deny"}'::jsonb, TRUE)
        """,
        f"{_SEED_TAG}-describe-deny-orders",
        json.dumps({"source_ids": [e2e_config.source_id_pg], "tables": ["orders"]}),
    )
    await _notify_policy(control_db)
    try:
        summary = _payload(
            mcp_call(
                e2e_config,
                "interlock_describe_access",
                {"source_id": e2e_config.source_id_pg},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        )
        tables = {
            entry["table"]: entry
            for entry in summary[e2e_config.source_id_pg]["resources"]["tables"]
        }
        assert tables["public.orders"]["policy"]["rule"] == f"{_SEED_TAG}-describe-deny-orders"
        assert "policy" not in tables["public.customers"]
        assert tables["public.customers"]["all_columns"] is True
    finally:
        await _cleanup(control_db)


async def test_a_non_sql_source_is_labelled_not_enforced(e2e_config: Any) -> None:
    summary = _payload(mcp_call(e2e_config, "interlock_describe_access", {}))
    labels = {
        source_id: entry.get("resources", {}).get("enforced")
        for source_id, entry in summary.items()
    }
    assert labels.get(e2e_config.source_id_pg) is True
    assert labels.get(e2e_config.source_id_http) is False, labels


async def test_a_legacy_public_role_is_described_as_reaching_its_table(
    control_db: Any, e2e_config: Any
) -> None:
    """A MySQL role written as `public.customers.*` - the only spelling that
    matched before catalog naming - still reads `customers` through the legacy
    alias, so describe_access must say it reaches `source_db.customers` and
    nothing else, rather than that it reaches nothing."""
    await _cleanup(control_db)
    role_id = await _role_with(
        control_db,
        source_id=e2e_config.source_id_mysql,
        role_key="describe_legacy_public",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "public.customers.*",
            }
        ],
    )
    api_key = await _identity(control_db, source_id=e2e_config.source_id_mysql, role_id=role_id)
    await _allow_policy(control_db, e2e_config.source_id_mysql)
    await _notify_policy(control_db)
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        read = mcp_call(
            e2e_config,
            "interlock_query",
            {"source_id": e2e_config.source_id_mysql, "sql": "SELECT id FROM customers LIMIT 1"},
            headers=headers,
        )
        assert read.status_code == 200, read.text
        summary = _payload(
            mcp_call(
                e2e_config,
                "interlock_describe_access",
                {"source_id": e2e_config.source_id_mysql},
                headers=headers,
            )
        )
        tables = [t["table"] for t in summary[e2e_config.source_id_mysql]["resources"]["tables"]]
        assert tables == ["source_db.customers"], tables
    finally:
        await _cleanup(control_db)
