"""Certification checks for the reusable compose-backed E2E environment."""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.support.seed import seed_all


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_compose_clean_boot_applies_all_migrations(control_db) -> None:
    migration_count = await control_db.fetchval("SELECT COUNT(*) FROM schema_migrations")
    assert migration_count >= 9

    columns = await control_db.fetch("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_name IN ('audit_log', 'ingestion_jobs', 'identities')
          AND column_name IN (
              'policy_decision',
              'approval_status',
              'updated_at',
              'lease_expires_at',
              'pg_username',
              'pg_password_hash'
          )
        """)
    found = {(row["table_name"], row["column_name"]) for row in columns}
    assert ("audit_log", "policy_decision") in found
    assert ("audit_log", "approval_status") in found
    assert ("ingestion_jobs", "updated_at") in found
    assert ("ingestion_jobs", "lease_expires_at") in found
    assert ("identities", "pg_username") in found
    assert ("identities", "pg_password_hash") in found


@pytest.mark.e2e
def test_gateway_admin_worker_health_with_seeded_stack(
    e2e_config,
    admin_session,
    http_upstream,
) -> None:
    with httpx.Client(timeout=10) as client:
        gateway = client.get(f"{e2e_config.gateway_url}/health")
        admin = client.get(f"{e2e_config.admin_url}/health")
    assert gateway.status_code == 200
    assert gateway.json()["status"] == "ok"
    assert admin.status_code == 200
    assert admin.json()["status"] == "ok"
    assert http_upstream.get("/health").json()["status"] == "ok"

    workers = admin_session.client.get("/api/ingestion/workers")
    assert workers.status_code == 200
    worker_ids = {worker["worker_id"] for worker in workers.json()}
    assert {"e2e-worker-1", "e2e-worker-2"} & worker_ids


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_compose_three_service_topology_is_independent(
    control_db,
    source_db,
    e2e_config,
) -> None:
    control_has_source_table = await control_db.fetchval("""
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'customers'
        )
        """)
    source_has_source_table = await source_db.fetchval("""
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'customers'
        )
        """)
    registered_source = await control_db.fetchval(
        "SELECT source_type FROM data_sources WHERE source_id = $1",
        e2e_config.source_id_pg,
    )
    assert control_has_source_table is False
    assert source_has_source_table is True
    assert registered_source == "postgresql"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_seed_data_is_idempotent(control_db, source_db, e2e_config) -> None:
    await seed_all(e2e_config)
    await seed_all(e2e_config)

    identity_count = await control_db.fetchval("""
        SELECT COUNT(*)
        FROM identities
        WHERE name IN ('e2e-agent', 'e2e-denied-agent')
          AND metadata->>'seed' = 'e2e'
        """)
    source_count = await control_db.fetchval(
        "SELECT COUNT(*) FROM data_sources WHERE metadata->>'seed' = 'e2e'"
    )
    policy_count = await control_db.fetchval(
        "SELECT COUNT(*) FROM policy_rules WHERE name LIKE 'e2e-%'"
    )
    customer_count = await source_db.fetchval("SELECT COUNT(*) FROM customers")

    assert identity_count == 2
    assert source_count == 9
    assert policy_count == 2
    assert customer_count == 2
