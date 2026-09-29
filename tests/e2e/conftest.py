"""Shared fixtures for E2E tests.

E2E tests require a running Docker Compose stack:
    make e2e-up

They are gated by ``-m e2e`` and additionally by ``INTERLOCK_E2E=1`` so they
do not run unintentionally in a unit-test pass.

Environment knobs:
    INTERLOCK_E2E              must be "1" or tests are skipped at fixture-level
    ADMIN_URL             default http://localhost:9090
    GATEWAY_URL           default http://localhost:3001
    E2E_*                deterministic seeded compose-stack knobs
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import httpx
import pytest
import pytest_asyncio

from tests.e2e.support.clients import (
    AdminSession,
    connect_control_db,
    connect_source_db,
    login_admin,
)
from tests.e2e.support.clients import (
    agent_headers as _agent_headers,
)
from tests.e2e.support.clients import (
    denied_headers as _denied_headers,
)
from tests.e2e.support.config import E2EConfig, load_e2e_config
from tests.e2e.support.seed import seed_all

E2E_ENABLED = os.environ.get("INTERLOCK_E2E", "0") == "1"
# Set by callers that seed on their own (see run-certification.sh).
SKIP_SEED = os.environ.get("INTERLOCK_E2E_SKIP_SEED", "0") == "1"


def pytest_collection_modifyitems(config, items):
    if E2E_ENABLED:
        return
    skip_marker = pytest.mark.skip(reason="INTERLOCK_E2E env var not set to 1")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def admin_url() -> str:
    return os.environ.get("ADMIN_URL", "http://localhost:9090")


@pytest.fixture(scope="session")
def gateway_url() -> str:
    return os.environ.get("GATEWAY_URL", "http://localhost:3001")


@pytest.fixture(scope="session")
def pg_params() -> dict[str, object]:
    return {
        "host": os.environ.get("PG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("PG_PORT", "5432")),
        "user": os.environ.get("PG_USER", "onyx"),
        "password": os.environ.get("PG_PASS", "onyx_dev"),
        "database": os.environ.get("PG_DB", "onyx"),
    }


@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    return load_e2e_config()


@pytest.fixture(scope="session", autouse=True)
def seeded_stack(e2e_config: E2EConfig) -> None:
    """Make the live compose stack deterministic for all E2E tests.

    Set ``INTERLOCK_E2E_SKIP_SEED=1`` when the caller has already seeded and
    the full compose stack is not present - the cloud certification run seeds
    explicitly against a port-forwarded cluster, where ``seed_all`` would
    otherwise try to reach compose-only upstreams a second time.
    """
    if not E2E_ENABLED or SKIP_SEED:
        return
    asyncio.run(seed_all(e2e_config))


@pytest_asyncio.fixture
async def control_db(e2e_config: E2EConfig) -> asyncpg.Connection:
    conn = await connect_control_db(e2e_config)
    try:
        yield conn
    finally:
        await conn.close()


async def ensure_catalogued(conn: asyncpg.Connection, source_id: str) -> None:
    """Scan `source_id` unless it already has a successful scan, and wait for it.

    Tests of catalog-backed behaviour must not depend on an earlier test file
    having scanned the source: on a fresh stack nothing has.
    """
    from interlock.catalog.queue import enqueue_catalog_scan

    if await conn.fetchval(
        "SELECT 1 FROM source_catalog_scans WHERE source_id = $1 AND status = 'succeeded'",
        source_id,
    ):
        return
    scan_id = await enqueue_catalog_scan(conn, source_id, trigger="manual")
    assert scan_id is not None, f"{source_id} cannot be catalogued"
    status = None
    for _ in range(120):
        status = await conn.fetchval(
            "SELECT status FROM source_catalog_scans WHERE id = $1", scan_id
        )
        if status in ("succeeded", "failed", "cancelled"):
            break
        await asyncio.sleep(0.5)
    assert status == "succeeded", f"catalog scan of {source_id} ended {status}"


@pytest_asyncio.fixture
async def catalogued_sql_sources(control_db: asyncpg.Connection, e2e_config: E2EConfig) -> None:
    for source_id in (e2e_config.source_id_pg, e2e_config.source_id_mysql):
        await ensure_catalogued(control_db, source_id)


@pytest_asyncio.fixture
async def source_db(e2e_config: E2EConfig) -> asyncpg.Connection:
    conn = await connect_source_db(e2e_config)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def admin_session(e2e_config: E2EConfig) -> AdminSession:
    session = login_admin(e2e_config)
    try:
        yield session
    finally:
        session.client.close()


@pytest.fixture
def agent_headers(e2e_config: E2EConfig) -> dict[str, str]:
    return _agent_headers(e2e_config)


@pytest.fixture
def denied_headers(e2e_config: E2EConfig) -> dict[str, str]:
    return _denied_headers(e2e_config)


@pytest.fixture
def pg_identity_params(e2e_config: E2EConfig) -> dict[str, object]:
    return e2e_config.gateway_pg_params_password


@pytest.fixture
def http_upstream(e2e_config: E2EConfig) -> httpx.Client:
    with httpx.Client(base_url=e2e_config.http_upstream_url, timeout=5) as client:
        client.get("/reset")
        yield client
