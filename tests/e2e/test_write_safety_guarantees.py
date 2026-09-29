"""Write-safety guarantees, judged by effect on the upstream.

Phase 2 of the governance audit, audited hardest because this is where the
shipped defect was.

The finding this file exists for: the `status = 'pending'` guard on the approve
statement is the only thing preventing an approval a human has already
*rejected* from being executed, and nothing tested it. Removing that one line
and approving a rejected approval returned ``{'status': 'approved',
'executed': True}`` and deleted the target row upstream. The control is intact
today - what was missing was any test that would notice if a refactor took it
out, which is precisely how the ``$2::text`` defect reached production.

Every assertion here reads the upstream, not the approval's status column. A
row that says `executed` is the system's account of itself; the audit only
accepts the row being gone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from interlock.core.approval_queue import ApprovalQueue
from interlock.errors import InterLockError
from tests.e2e.support import effects

pytestmark = [pytest.mark.e2e]

_MARKER_BASE = 770000


async def _seed_marker(e2e_config: Any, marker_id: int) -> None:
    """Put a known row upstream so its survival is meaningful."""
    import aiomysql

    conn = await aiomysql.connect(
        host=e2e_config.source_mysql_host,
        port=e2e_config.source_mysql_port,
        user=e2e_config.source_mysql_user,
        password=e2e_config.source_mysql_password,
        db=e2e_config.source_mysql_database,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM customers WHERE id = %s", (marker_id,))
            await cur.execute(
                "INSERT INTO customers (id, name, email, ssn, note)" " VALUES (%s, %s, %s, %s, %s)",
                (marker_id, "Audit Probe", "probe@example.com", "000-00-0000", "probe"),
            )
        await conn.commit()
    finally:
        conn.close()


async def _marker_survives(e2e_config: Any, marker_id: int) -> bool:
    rows = await effects.mysql_rows(e2e_config, "SELECT id FROM customers WHERE id = %s", marker_id)
    return bool(rows)


async def _queue_approval(
    conn: Any, marker_id: int, *, status: str = "pending", expires_in_minutes: int = 30
) -> int:
    return int(
        await conn.fetchval(
            """
            INSERT INTO write_approval_queue
                (identity_id, source_id, sql_text, risk_level, status,
                 expires_at, request_metadata)
            VALUES (1, 'e2e_mysql', $1, 'medium', $2, $3, $4::jsonb)
            RETURNING id
            """,
            f"DELETE FROM customers WHERE id = {marker_id}",
            status,
            datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
            json.dumps({"protocol": "mcp", "identity_id": 1}),
        )
    )


@pytest.fixture
async def approval_queue(e2e_config: Any) -> Any:
    """An ApprovalQueue wired the way the admin wires it, reachable from the host.

    The stored source config addresses MySQL by its compose hostname, which the
    containers resolve and the host does not. Only the network address is
    rewritten; connector selection and the execution path are untouched.
    """
    import asyncpg

    from interlock.metadata.registry import MetadataRegistry

    async def init(connection: Any) -> None:
        await connection.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    pool = await asyncpg.create_pool(e2e_config.control_dsn, min_size=1, max_size=2, init=init)
    try:
        registry = MetadataRegistry(pool)
        await registry.load()
        source = registry.get("e2e_mysql")
        assert source is not None, "seed did not register e2e_mysql"
        source.connection_config["host"] = e2e_config.source_mysql_host
        source.connection_config["port"] = e2e_config.source_mysql_port
        yield ApprovalQueue(pool, None, registry=registry), pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_rejected_approval_can_never_be_executed(
    e2e_config: Any, control_db: Any, approval_queue: Any
) -> None:
    """A human's refusal must be final.

    This is the finding Phase 2 was written for. The `status = 'pending'` guard
    is the only thing standing between a rejected approval and execution;
    removing it let a rejected DELETE run and destroy the row. Nothing covered
    it, so a refactor of that statement could have re-enabled a governance
    bypass silently.
    """
    queue, _pool = approval_queue
    marker_id = _MARKER_BASE + 1
    await _seed_marker(e2e_config, marker_id)
    approval_id = await _queue_approval(control_db, marker_id, status="rejected")

    try:
        with pytest.raises(InterLockError):
            await queue.approve(approval_id, "auditor")

        assert await _marker_survives(e2e_config, marker_id), (
            "a REJECTED write was executed against the upstream - a human refusal" " was overridden"
        )
        row = await control_db.fetchrow(
            "SELECT status, executed_at FROM write_approval_queue WHERE id = $1", approval_id
        )
        assert row["status"] == "rejected", f"rejection was overwritten: {row['status']}"
        assert row["executed_at"] is None
    finally:
        await control_db.execute("DELETE FROM write_approval_queue WHERE id = $1", approval_id)


@pytest.mark.asyncio
async def test_an_already_executed_approval_cannot_execute_twice(
    e2e_config: Any, control_db: Any, approval_queue: Any
) -> None:
    """Approving twice must not run the write twice.

    Deleting the same row twice is harmless; an INSERT or a transfer is not.
    The same guard protects both, so it is asserted here on the shape the
    fixtures allow.
    """
    queue, _pool = approval_queue
    marker_id = _MARKER_BASE + 2
    await _seed_marker(e2e_config, marker_id)
    approval_id = await _queue_approval(control_db, marker_id)

    try:
        first = await queue.approve(approval_id, "auditor")
        assert first["executed"] is True, f"first approval did not execute: {first}"
        assert not await _marker_survives(e2e_config, marker_id)

        with pytest.raises(InterLockError):
            await queue.approve(approval_id, "auditor")

        row = await control_db.fetchrow(
            "SELECT status FROM write_approval_queue WHERE id = $1", approval_id
        )
        assert (
            row["status"] == "executed"
        ), f"a second approval changed the terminal state to {row['status']!r}"
    finally:
        await control_db.execute("DELETE FROM write_approval_queue WHERE id = $1", approval_id)


@pytest.mark.asyncio
async def test_an_expired_approval_cannot_be_executed(
    e2e_config: Any, control_db: Any, approval_queue: Any
) -> None:
    """An approval past its expiry must not run, and the row must survive."""
    queue, _pool = approval_queue
    marker_id = _MARKER_BASE + 3
    await _seed_marker(e2e_config, marker_id)
    approval_id = await _queue_approval(control_db, marker_id, expires_in_minutes=-5)

    try:
        with pytest.raises(InterLockError):
            await queue.approve(approval_id, "auditor")

        assert await _marker_survives(
            e2e_config, marker_id
        ), "an expired approval was executed against the upstream"
    finally:
        await control_db.execute("DELETE FROM write_approval_queue WHERE id = $1", approval_id)


@pytest.mark.asyncio
async def test_an_approved_write_reaches_the_upstream(
    e2e_config: Any, control_db: Any, approval_queue: Any
) -> None:
    """The positive case, judged by effect.

    Without this the negative tests above prove nothing: they would all pass
    against a system that never executes anything at all - which is exactly
    the state the shipped defect left it in.
    """
    queue, _pool = approval_queue
    marker_id = _MARKER_BASE + 4
    await _seed_marker(e2e_config, marker_id)
    assert await _marker_survives(e2e_config, marker_id), "probe row was not seeded"
    approval_id = await _queue_approval(control_db, marker_id)

    try:
        result = await queue.approve(approval_id, "auditor")
        assert result["executed"] is True, f"approval reported {result}"
        assert not await _marker_survives(
            e2e_config, marker_id
        ), "the approval reported executed but the upstream row survived"
    finally:
        await control_db.execute("DELETE FROM write_approval_queue WHERE id = $1", approval_id)
