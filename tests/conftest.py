"""Shared pytest fixtures for Onyx test suite.

Provides:
- Session-scoped PostgreSQL connection pool (integration tests)
- Session-scoped Redis client (integration tests)
- Session-scoped migration runner (applies SQL migrations to test DB)
- Function-scoped test data cleanup (truncates tables after each test)
- InterLockConfig fixture with sensible defaults

Integration fixtures check connectivity and skip gracefully when
PG or Redis are unavailable. They also require INTEGRATION_TEST=1.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

_INTEGRATION_ENABLED = os.environ.get("INTEGRATION_TEST", "0") == "1"

integration = pytest.mark.skipif(
    not _INTEGRATION_ENABLED,
    reason="INTEGRATION_TEST env var not set to 1",
)


# ---------------------------------------------------------------------------
# Test database connection parameters
# ---------------------------------------------------------------------------

_TEST_PG_HOST = os.environ.get("TEST_PG_HOST", "localhost")
_TEST_PG_PORT = int(os.environ.get("TEST_PG_PORT", "5433"))
_TEST_PG_USER = os.environ.get("TEST_PG_USER", "onyx")
_TEST_PG_PASSWORD = os.environ.get("TEST_PG_PASSWORD", "onyx_dev")
_TEST_PG_DATABASE = os.environ.get("TEST_PG_DATABASE", "onyx")

_TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Tables created by the initial migration, in dependency-safe truncation order.
# audit_log is partitioned so we use DELETE instead of TRUNCATE for it.
_TABLES_TO_TRUNCATE = [
    "entity_document_xref",
    "ingestion_jobs",
    "discovery_assets",
    "category_taxonomy",
    "write_approval_queue",
    "schema_catalog",
    "cache_policies",
    "policy_rules",
    "identities",
    "classification_tags",
    "data_sources",
]


async def _pg_is_reachable() -> bool:
    """Return True if the test Postgres instance accepts connections."""
    try:
        import asyncpg

        conn = await asyncpg.connect(
            host=_TEST_PG_HOST,
            port=_TEST_PG_PORT,
            user=_TEST_PG_USER,
            password=_TEST_PG_PASSWORD,
            database=_TEST_PG_DATABASE,
            timeout=5,
        )
        await conn.close()
        return True
    except Exception:
        return False


def _redis_is_reachable() -> bool:
    """Return True if the test Redis instance accepts connections."""
    try:
        import redis

        client = redis.Redis.from_url(_TEST_REDIS_URL, socket_connect_timeout=5)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# InterLockConfig fixture (always available)
# ---------------------------------------------------------------------------


@pytest.fixture()
def onyx_config():
    """Return an InterLockConfig with default values suitable for tests."""
    from interlock.config import InterLockConfig

    return InterLockConfig(
        database={"host": _TEST_PG_HOST, "port": _TEST_PG_PORT, "user": _TEST_PG_USER, "password": _TEST_PG_PASSWORD, "database": _TEST_PG_DATABASE},  # type: ignore[arg-type]
        redis={"url": _TEST_REDIS_URL},  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# PostgreSQL pool (session-scoped, integration only)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
async def pg_pool():
    """Create an asyncpg connection pool to the test database.

    Skips the entire session if PG is not reachable or INTEGRATION_TEST != 1.
    """
    if not _INTEGRATION_ENABLED:
        pytest.skip("INTEGRATION_TEST env var not set to 1")

    if not await _pg_is_reachable():
        pytest.skip(f"PostgreSQL not reachable at {_TEST_PG_HOST}:{_TEST_PG_PORT}")

    import asyncpg

    pool = await asyncpg.create_pool(
        host=_TEST_PG_HOST,
        port=_TEST_PG_PORT,
        user=_TEST_PG_USER,
        password=_TEST_PG_PASSWORD,
        database=_TEST_PG_DATABASE,
        min_size=1,
        max_size=5,
    )
    assert pool is not None
    yield pool
    await pool.close()


# ---------------------------------------------------------------------------
# Redis client (session-scoped, integration only)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def redis_client():
    """Create a Redis client connected to the test instance.

    Skips the entire session if Redis is not reachable or INTEGRATION_TEST != 1.
    """
    if not _INTEGRATION_ENABLED:
        pytest.skip("INTEGRATION_TEST env var not set to 1")

    if not _redis_is_reachable():
        pytest.skip(f"Redis not reachable at {_TEST_REDIS_URL}")

    import redis

    client = redis.Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Migration runner (session-scoped, integration only)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
async def run_migrations(pg_pool):
    """Execute all SQL migration files against the test database.

    Migrations are read from the migrations/ directory and executed in
    lexicographic order. This fixture depends on pg_pool and therefore
    inherits the integration-test skip behaviour.
    """
    if not _MIGRATIONS_DIR.is_dir():
        pytest.skip(f"Migrations directory not found: {_MIGRATIONS_DIR}")

    sql_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        pytest.skip("No migration files found")

    async with pg_pool.acquire() as conn:
        for sql_file in sql_files:
            sql = sql_file.read_text()
            await conn.execute(sql)

    return True


# ---------------------------------------------------------------------------
# Test data cleanup (function-scoped, integration only)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def clean_tables(pg_pool):
    """Truncate all application tables after each test function.

    Yields control to the test, then cleans up. Depends on pg_pool so it
    inherits the integration-test skip behaviour.
    """
    yield

    async with pg_pool.acquire() as conn:
        # audit_log is partitioned - DELETE is safer than TRUNCATE
        await conn.execute("DELETE FROM audit_log")

        for table in _TABLES_TO_TRUNCATE:
            await conn.execute(f"TRUNCATE TABLE {table} CASCADE")
