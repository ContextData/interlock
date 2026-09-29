"""Approval execution certification against a real control database.

These paths were previously covered only by unit tests using AsyncMock pools,
which accept any SQL string. That hid two defects: the statement recording an
execution failure could not be prepared at all, and approvals for non-
PostgreSQL sources were attempted over the PostgreSQL wire protocol. Both left
a row at status='approved' that the console rendered as a completed write.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from interlock.core.approval_queue import ApprovalQueue
from interlock.errors import InterLockError, WriteApprovalRequiredError
from tests.e2e.support.clients import connect_control_db
from tests.e2e.support.config import E2EConfig

pytestmark = [pytest.mark.e2e]

_PROBE_SQL = "DELETE FROM customers WHERE id = -20260829"


async def _init_codecs(conn: asyncpg.Connection) -> None:
    """Mirror interlock.db.pool, which registers these on every connection."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class _FailingConnectionManager:
    """Stands in for a source whose upstream refuses the write."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def get_pool(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self._error


@pytest.fixture
async def control_pool(e2e_config: E2EConfig) -> Any:
    pool = await asyncpg.create_pool(
        e2e_config.control_dsn, min_size=1, max_size=2, init=_init_codecs
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def control_conn(e2e_config: E2EConfig) -> Any:
    conn = await connect_control_db(e2e_config)
    try:
        yield conn
    finally:
        await conn.execute("DELETE FROM write_approval_queue WHERE sql_text = $1", _PROBE_SQL)
        await conn.close()


@pytest.mark.asyncio
async def test_failed_execution_is_recorded_instead_of_reading_as_approved(
    control_pool: Any, control_conn: Any
) -> None:
    """A write that cannot execute must land at 'failed', never 'approved'.

    Guards the IndeterminateDatatypeError in the failure-recording statement:
    jsonb_build_object is variadic "any", so the error parameter needs an
    explicit cast or the UPDATE cannot be prepared at all. While that was
    broken the row kept the 'approved' status written moments earlier, and the
    console reported a governed write that never ran.
    """
    approval_id = int(
        await control_conn.fetchval(
            """
            INSERT INTO write_approval_queue
                (identity_id, source_id, sql_text, risk_level, status,
                 expires_at, request_metadata)
            VALUES ($1, $2, $3, 'medium', 'pending', $4, $5::jsonb)
            RETURNING id
            """,
            1,
            "e2e_pg",
            _PROBE_SQL,
            datetime.now(UTC) + timedelta(minutes=10),
            json.dumps({"protocol": "postgresql", "identity_id": 1}),
        )
    )

    queue = ApprovalQueue(
        control_pool,
        _FailingConnectionManager(RuntimeError("upstream refused the write")),
    )
    with pytest.raises(InterLockError, match="failed during execution"):
        await queue.approve(approval_id, "certification")

    row = await control_conn.fetchrow(
        "SELECT status, executed_at, request_metadata FROM write_approval_queue WHERE id = $1",
        approval_id,
    )
    assert row["status"] == "failed", (
        f"expected the failure to be recorded, got status={row['status']!r};"
        " an approved-but-unexecuted row reads as a completed write in the console"
    )
    assert row["executed_at"] is None

    metadata = row["request_metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert isinstance(metadata, dict), f"metadata corrupted into {type(metadata).__name__}"
    assert "upstream refused the write" in metadata["execution_error"]
    assert metadata["failed_at"]


@pytest.mark.asyncio
async def test_submitted_metadata_is_stored_as_a_json_object(
    control_pool: Any, control_conn: Any
) -> None:
    """Metadata must be a jsonb object, not a double-encoded JSON string.

    submit() called json.dumps on metadata that the pool's jsonb codec then
    encoded again, so the column held a JSON string. Concatenating an object
    onto such a value yields an array rather than an error, so the failure
    path silently corrupted the record it was trying to annotate.
    """
    from interlock.models import RiskLevel

    queue = ApprovalQueue(control_pool, None)
    # submit() signals the queued write by raising; the stored row is the
    # subject of this test.
    with pytest.raises(WriteApprovalRequiredError):
        await queue.submit(
            identity_id=1,
            source_id="e2e_pg",
            sql_text=_PROBE_SQL,
            risk_level=RiskLevel.MEDIUM,
            metadata={"protocol": "postgresql", "probe": "metadata-shape"},
        )

    row = await control_conn.fetchrow(
        "SELECT jsonb_typeof(request_metadata) AS shape, request_metadata"
        " FROM write_approval_queue WHERE sql_text = $1 ORDER BY id DESC LIMIT 1",
        _PROBE_SQL,
    )
    assert row is not None, "submit() did not create a row"
    assert row["shape"] == "object", (
        f"request_metadata stored as {row['shape']!r};"
        " a double-encoded string breaks every jsonb operator that reads it"
    )


@pytest.mark.asyncio
async def test_approved_write_executes_against_a_mysql_source(
    control_pool: Any, control_conn: Any, e2e_config: E2EConfig
) -> None:
    """A MySQL approval must actually run against MySQL.

    ConnectionManager hands out asyncpg pools, so every non-HTTP approval used
    to be attempted over the PostgreSQL wire protocol: a MySQL write failed
    with 'PostgreSQL server at source-mysql:3306 rejected SSL upgrade' and the
    row was left reading as approved. This asserts the upstream row is really
    gone, not merely that the status changed.
    """
    import aiomysql

    from interlock.metadata.registry import MetadataRegistry

    marker_id = 987654
    mysql = await aiomysql.connect(
        host=e2e_config.source_mysql_host,
        port=e2e_config.source_mysql_port,
        user=e2e_config.source_mysql_user,
        password=e2e_config.source_mysql_password,
        db=e2e_config.source_mysql_database,
    )
    try:
        async with mysql.cursor() as cur:
            await cur.execute("DELETE FROM customers WHERE id = %s", (marker_id,))
            await cur.execute(
                "INSERT INTO customers (id, name, email, ssn, note)" " VALUES (%s, %s, %s, %s, %s)",
                (marker_id, "Approval Probe", "probe@example.com", "000-00-0000", "probe row"),
            )
        await mysql.commit()

        registry = MetadataRegistry(control_pool)
        await registry.load()

        # The stored config addresses MySQL by its compose hostname, which the
        # containers resolve and the host does not. Rewrite only the network
        # address: the connector selection and execution path under test are
        # untouched.
        source = registry.get("e2e_mysql")
        assert source is not None, "seed did not register e2e_mysql"
        source.connection_config["host"] = e2e_config.source_mysql_host
        source.connection_config["port"] = e2e_config.source_mysql_port

        sql = f"DELETE FROM customers WHERE id = {marker_id}"
        approval_id = int(
            await control_conn.fetchval(
                """
                INSERT INTO write_approval_queue
                    (identity_id, source_id, sql_text, risk_level, status,
                     expires_at, request_metadata)
                VALUES ($1, $2, $3, 'medium', 'pending', $4, $5::jsonb)
                RETURNING id
                """,
                1,
                "e2e_mysql",
                sql,
                datetime.now(UTC) + timedelta(minutes=10),
                json.dumps({"protocol": "mcp", "identity_id": 1}),
            )
        )

        try:
            queue = ApprovalQueue(control_pool, None, registry=registry)
            result = await queue.approve(approval_id, "certification")
            assert result["executed"] is True, f"approval reported {result}"

            row = await control_conn.fetchrow(
                "SELECT status, executed_at FROM write_approval_queue WHERE id = $1",
                approval_id,
            )
            assert row["status"] == "executed"
            assert row["executed_at"] is not None

            async with mysql.cursor() as cur:
                await cur.execute("SELECT count(*) FROM customers WHERE id = %s", (marker_id,))
                remaining = (await cur.fetchone())[0]
            assert (
                remaining == 0
            ), "the approved DELETE was reported as executed but the upstream row survived"
        finally:
            await control_conn.execute(
                "DELETE FROM write_approval_queue WHERE id = $1", approval_id
            )
    finally:
        async with mysql.cursor() as cur:
            await cur.execute("DELETE FROM customers WHERE id = %s", (marker_id,))
        await mysql.commit()
        mysql.close()
