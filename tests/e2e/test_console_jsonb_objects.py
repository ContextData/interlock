"""The console stores JSON as objects, and migration 015 repairs rows that
were stored as JSON strings - proven against the real control-plane pool.

Found on a live test deployment. The pools register a jsonb codec that
serialises parameters, and console writers serialised them first, so every
console-created source, identity, role and permission stored a JSON *string*.
The Data Sources pages then returned 500. Unit tests use fake pools without
the codec, and no e2e test created a source through the console, so nothing
caught it; this file closes that gap.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.e2e]

MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "015_repair_double_encoded_jsonb.sql"
)


async def _cleanup(control_db: Any, source_id: str) -> None:
    await control_db.execute(
        "UPDATE data_sources SET enabled = FALSE WHERE source_id = $1", source_id
    )
    await control_db.execute(
        "DELETE FROM source_role_permissions WHERE role_id IN "
        "(SELECT id FROM source_roles WHERE source_id = $1)",
        source_id,
    )
    await control_db.execute("DELETE FROM source_roles WHERE source_id = $1", source_id)
    await control_db.execute("DELETE FROM data_sources WHERE source_id = $1", source_id)
    await control_db.execute("NOTIFY onyx_config_changed")


async def test_a_console_created_source_is_stored_as_objects_and_its_pages_load(
    e2e_config: Any, admin_session: Any, control_db: Any
) -> None:
    source_id = f"e2e_console_{secrets.token_hex(4)}"
    try:
        created = admin_session.client.post(
            "/dashboard/data-sources/create",
            data={
                "source_id": source_id,
                "name": "E2E console source",
                "source_type": "postgresql",
                "connector_key": "postgresql",
                "cache_strategy": "bypass",
                "host": e2e_config.compose_source_pg_host,
                "port": "5432",
                "database": e2e_config.source_pg_database,
                "user": e2e_config.source_pg_user,
                "sslmode": "disable",
                "create_default_roles": "on",
            },
            headers={"X-CSRF-Token": admin_session.csrf_token},
        )
        assert created.status_code == 200, created.text[:300]

        types = await control_db.fetchrow(
            "SELECT jsonb_typeof(metadata) AS metadata, jsonb_typeof(connection_config) AS config "
            "FROM data_sources WHERE source_id = $1",
            source_id,
        )
        assert dict(types) == {"metadata": "object", "config": "object"}
        role_types = await control_db.fetch(
            "SELECT DISTINCT jsonb_typeof(r.metadata) AS role_meta, jsonb_typeof(p.constraints) AS constraints "
            "FROM source_roles r JOIN source_role_permissions p ON p.role_id = r.id "
            "WHERE r.source_id = $1",
            source_id,
        )
        assert role_types, "default role templates were not created"
        assert {(r["role_meta"], r["constraints"]) for r in role_types} == {("object", "object")}

        for path in (
            "/dashboard/data-sources",
            f"/dashboard/data-sources/{source_id}",
            f"/dashboard/data-sources/{source_id}/edit",
        ):
            page = admin_session.client.get(path)
            assert page.status_code == 200, f"{path} -> {page.status_code}"
        edit = admin_session.client.get(f"/dashboard/data-sources/{source_id}/edit")
        assert 'name="config_key" value="sslmode"' in edit.text
    finally:
        await _cleanup(control_db, source_id)


async def test_migration_015_turns_json_strings_back_into_objects(control_db: Any) -> None:
    broken = f"e2e_jsonb_broken_{secrets.token_hex(4)}"
    opaque = f"e2e_jsonb_opaque_{secrets.token_hex(4)}"
    try:
        # Exactly what the defect stored: a JSON string whose text is an object.
        await control_db.execute(
            "INSERT INTO data_sources (source_id, name, source_type, connection_config, "
            "cache_strategy, enabled, metadata) "
            "VALUES ($1, 'broken', 'postgresql', to_jsonb($2::text), 'bypass', FALSE, to_jsonb($3::text))",
            broken,
            json.dumps({"host": "db.example.com"}),
            json.dumps({"connector_key": "postgresql"}),
        )
        # A string that is not JSON must be left alone, not fail the upgrade.
        await control_db.execute(
            "INSERT INTO data_sources (source_id, name, source_type, connection_config, "
            "cache_strategy, enabled, metadata) "
            "VALUES ($1, 'opaque', 'postgresql', '{}'::jsonb, 'bypass', FALSE, to_jsonb('not json'::text))",
            opaque,
        )

        await control_db.execute(MIGRATION.read_text())

        rows = {
            r["source_id"]: dict(r)
            for r in await control_db.fetch(
                "SELECT source_id, jsonb_typeof(metadata) AS metadata, "
                "jsonb_typeof(connection_config) AS config, metadata->>'connector_key' AS key "
                "FROM data_sources WHERE source_id = ANY($1)",
                [broken, opaque],
            )
        }
        assert rows[broken] == {
            "source_id": broken,
            "metadata": "object",
            "config": "object",
            "key": "postgresql",
        }
        assert rows[opaque]["metadata"] == "string"

        # Running it again changes nothing: it is safe on an already-repaired database.
        await control_db.execute(MIGRATION.read_text())
        again = await control_db.fetchval(
            "SELECT jsonb_typeof(metadata) FROM data_sources WHERE source_id = $1", broken
        )
        assert again == "object"
    finally:
        await control_db.execute(
            "DELETE FROM data_sources WHERE source_id = ANY($1)", [broken, opaque]
        )
