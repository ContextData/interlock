"""Seeded PostgreSQL proxy E2E certification tests."""

from __future__ import annotations

import pytest

from tests.e2e.support.assertions import assert_audit_event
from tests.e2e.support.clients import latest_audit, wait_for


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pg_api_key_password_authenticates_before_upstream(e2e_config) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(**e2e_config.gateway_pg_params_api_key, timeout=10)
    try:
        value = await conn.fetchval("SELECT 1")
    finally:
        await conn.close()
    assert value == 1


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pg_dedicated_password_authenticates_identity(e2e_config) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(**e2e_config.gateway_pg_params_password, timeout=10)
    try:
        value = await conn.fetchval("SELECT 2")
    finally:
        await conn.close()
    assert value == 2


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pg_query_uses_registered_source_not_control_db(e2e_config) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(**e2e_config.gateway_pg_params_password, timeout=10)
    try:
        name = await conn.fetchval("SELECT name FROM customers WHERE id = 1")
    finally:
        await conn.close()
    assert name == "Ada Lovelace"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pg_simple_select_redacts_and_audits(e2e_config, control_db) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(**e2e_config.gateway_pg_params_password, timeout=10)
    try:
        value = await conn.fetchval("SELECT ssn FROM customers WHERE id = 1")
    finally:
        await conn.close()

    assert value != "123-45-6789"

    row = await wait_for(
        lambda: latest_audit(
            control_db,
            source_id=e2e_config.source_id_pg,
            protocol="postgresql",
            status="success",
        )
    )
    assert_audit_event(
        row,
        source_id=e2e_config.source_id_pg,
        protocol="postgresql",
        status="success",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pg_extended_prepared_data_modifying_cte_is_blocked(e2e_config) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(**e2e_config.gateway_pg_params_password, timeout=10)
    try:
        with pytest.raises(Exception, match="Write|write|approval|permission|blocked"):
            await conn.prepare(
                "WITH del AS (DELETE FROM customers WHERE id = 2 RETURNING *) " "SELECT * FROM del"
            )
    finally:
        await conn.close()
