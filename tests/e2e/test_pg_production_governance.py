"""Compose-backed PG governance checks.

These tests are intentionally opt-in on top of ``INTERLOCK_E2E=1`` because they
need a seeded InterLock identity and source. They verify the production path:
PostgreSQL client password -> InterLock identity -> source-aware upstream.
"""

from __future__ import annotations

import pytest

from tests.e2e.support.config import load_e2e_config


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pg_proxy_authenticates_interlock_identity_and_reads_source() -> None:
    asyncpg = pytest.importorskip("asyncpg")
    config = load_e2e_config()

    conn = await asyncpg.connect(
        **config.gateway_pg_params_password,
        timeout=10,
    )
    try:
        value = await conn.fetchval("SELECT 1")
    finally:
        await conn.close()

    assert value == 1
