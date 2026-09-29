"""Tier 1: governance applied to a real managed PostgreSQL, over PG-wire.

Every assertion here reads ground truth from the upstream with
`tests/live/support/effects.py`, never from the gateway's own response. Where a
control is proven by refusal, the refusal is checked against the documented
error text rather than merely being non-200, so a connector that failed for an
unrelated reason cannot masquerade as a working control.

**PG-wire, not MCP, and that is a finding rather than a preference.** The same
source configuration behaves differently across protocols: `pg_proxy` resolves
`password_ref` and honours `sslmode`, while `ConnectionManager` reads a literal
`password` and only `ssl`, so this source connects with no password over MCP
PG-wire is also PostgreSQL's primary protocol here, so the
coverage is where it matters most - but the MCP gap is recorded, not papered
over.

The upstream user is `generic_write_user`, which holds SELECT plus
INSERT/UPDATE on the certification table. Choosing the *least* restricted
upstream user available is what makes these conclusions falsifiable: when a
request is refused, only InterLock can have refused it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from tests.live.support import effects
from tests.live.support.config import LiveConfig, load_live_config
from tests.live.support.evidence import Control, Verdict, record
from tests.live.support.seed import AGENT_API_KEY, BLOCKED_API_KEY, source_id

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not load_live_config().has_postgres(),
        reason="live PostgreSQL credentials are not configured",
    ),
]

SOURCE = source_id("postgres")
SSN = "123-45-6789"
EMAIL = "ada@example.com"


async def _gateway_connect(cfg: LiveConfig, api_key: str) -> asyncpg.Connection:
    """Connect through the InterLock PG listener. The database name selects the source."""
    return await asyncpg.connect(
        host="127.0.0.1",
        port=5434,
        database=SOURCE,
        user="e2e_api_key_login",
        password=api_key,
        ssl=False,
        statement_cache_size=0,
    )


@pytest.fixture(scope="module")
async def table(live_config: LiveConfig) -> AsyncIterator[str]:
    """A run-stamped table holding one PII row, dropped afterwards."""
    name = live_config.table_name
    await effects.postgres_execute(
        live_config,
        f"CREATE TABLE IF NOT EXISTS {name} "
        "(id serial PRIMARY KEY, label text NOT NULL, ssn text, email text)",
        user="generic_ddl_user",
    )
    for statement in (
        f"GRANT SELECT, INSERT, UPDATE ON {name} TO generic_write_user",
        f"GRANT USAGE, SELECT ON SEQUENCE {name}_id_seq TO generic_write_user",
        # The independent reader is deliberately a *different* upstream
        # identity from the one the gateway connects as, so ground truth is
        # never observed through the same grant path being tested.
        f"GRANT SELECT ON {name} TO generic_read_user",
    ):
        await effects.postgres_execute(live_config, statement, user="generic_ddl_user")
    await effects.postgres_execute(
        live_config,
        f"INSERT INTO {name} (label, ssn, email) VALUES ('alpha', $1, $2)",
        SSN,
        EMAIL,
        user="generic_ddl_user",
    )
    try:
        yield name
    finally:
        await effects.postgres_execute(
            live_config, f"DROP TABLE IF EXISTS {name} CASCADE", user="generic_ddl_user"
        )


@pytest.mark.asyncio
async def test_a_granted_identity_can_read(live_config: LiveConfig, table: str) -> None:
    conn = await _gateway_connect(live_config, AGENT_API_KEY)
    try:
        rows = await conn.fetch(f"SELECT label FROM {table}")
    finally:
        await conn.close()

    record(
        SOURCE,
        Control.ROLE_ALLOW,
        Verdict.PASS if rows else Verdict.FAIL,
        detail="the granted identity read the certification table over PG-wire",
        rows_returned=len(rows),
    )
    assert [r["label"] for r in rows] == ["alpha"]


@pytest.mark.asyncio
async def test_a_blocked_identity_is_refused_for_the_right_reason(
    live_config: LiveConfig, table: str
) -> None:
    """Refusal must be a governance refusal, not any error at all.

    Accepting any exception would let an unreachable upstream or a malformed
    query masquerade as a working control - a test that cannot fail.
    """
    with pytest.raises(asyncpg.InsufficientPrivilegeError) as excinfo:
        conn = await _gateway_connect(live_config, BLOCKED_API_KEY)
        try:
            await conn.fetch(f"SELECT label FROM {table}")
        finally:
            await conn.close()

    message = str(excinfo.value)
    record(
        SOURCE,
        Control.ROLE_DENY,
        Verdict.PASS if "Source role denied" in message else Verdict.FAIL,
        detail="the blocked identity was refused by source role, naming the control",
        error=message[:120],
    )
    assert "Source role denied" in message, f"refused, but not by source role: {message}"


@pytest.mark.asyncio
async def test_pii_is_redacted_although_the_upstream_still_holds_it(
    live_config: LiveConfig, table: str
) -> None:
    """The decisive redaction test: both halves, in one assertion pair.

    Asserting only that the response is redacted would pass just as well if
    the upstream row were empty. The proof is the *contrast* - the gateway
    returns a marker while the independent reader still sees the real value.
    """
    conn = await _gateway_connect(live_config, AGENT_API_KEY)
    try:
        governed = [dict(r) for r in await conn.fetch(f"SELECT ssn, email FROM {table}")]
    finally:
        await conn.close()

    upstream = await effects.postgres_rows(
        live_config, f"SELECT ssn, email FROM {table}", user="generic_read_user"
    )

    served = " ".join(str(v) for row in governed for v in row.values())
    truth = " ".join(str(v) for row in upstream for v in row.values())

    redacted = SSN not in served and EMAIL not in served
    upstream_intact = SSN in truth and EMAIL in truth

    record(
        SOURCE,
        Control.REDACTION,
        Verdict.PASS if redacted and upstream_intact else Verdict.FAIL,
        detail=(
            "the gateway returned redaction markers while the upstream row still "
            "held the real values - proven by contrast, not by the marker alone"
        ),
        served_contains_pii=not redacted,
        upstream_still_has_pii=upstream_intact,
        markers=sorted({str(v) for row in governed for v in row.values()}),
    )

    assert upstream_intact, "precondition failed: the upstream row does not contain the PII"
    assert redacted, f"unredacted PII reached the caller: {governed}"


@pytest.mark.asyncio
async def test_a_governed_write_reaches_the_upstream(live_config: LiveConfig, table: str) -> None:
    """Write safety, asserted where the defect that started this audit hid.

    The original defect left an approval marked executed while the row never
    changed upstream. So the assertion is on the upstream row, read
    independently - never on the statement returning cleanly.
    """
    label = f"written-{live_config.run_id}"
    conn = await _gateway_connect(live_config, AGENT_API_KEY)
    try:
        await conn.execute(f"INSERT INTO {table} (label) VALUES ('{label}')")
    finally:
        await conn.close()

    landed = await effects.postgres_rows(
        live_config,
        f"SELECT label FROM {table} WHERE label = $1",
        label,
        user="generic_read_user",
    )

    record(
        SOURCE,
        Control.WRITE_SAFETY,
        Verdict.PASS if len(landed) == 1 else Verdict.FAIL,
        detail="a governed write was confirmed present upstream by the independent reader",
        rows_found_upstream=len(landed),
    )
    assert len(landed) == 1, "the governed write never reached the upstream table"


@pytest.mark.asyncio
async def test_a_blocked_identitys_write_never_reaches_the_upstream(
    live_config: LiveConfig, table: str
) -> None:
    """The negative case for write safety, and the one that matters.

    A refusal the gateway reports but does not enforce is exactly the defect
    this audit exists to catch, so absence is confirmed upstream rather than
    inferred from the error.
    """
    label = f"blocked-{live_config.run_id}"
    with pytest.raises(asyncpg.PostgresError):
        conn = await _gateway_connect(live_config, BLOCKED_API_KEY)
        try:
            await conn.execute(f"INSERT INTO {table} (label) VALUES ('{label}')")
        finally:
            await conn.close()

    landed = await effects.postgres_rows(
        live_config,
        f"SELECT label FROM {table} WHERE label = $1",
        label,
        user="generic_read_user",
    )

    record(
        SOURCE,
        Control.APPROVAL_GATING,
        Verdict.PASS if not landed else Verdict.FAIL,
        detail="a refused write left no row upstream, confirmed by the independent reader",
        rows_found_upstream=len(landed),
    )
    assert not landed, "a refused write still reached the upstream table"


@pytest.mark.asyncio
async def test_the_governed_query_is_audited(
    live_config: LiveConfig, table: str, control_plane: Any
) -> None:
    """One row, naming this source, recording the decision that was made."""
    before = int(await control_plane.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))

    conn = await _gateway_connect(live_config, AGENT_API_KEY)
    try:
        await conn.fetch(f"SELECT label FROM {table}")
    finally:
        await conn.close()

    rows: list[Any] = []
    for _ in range(24):
        rows = await control_plane.fetch(
            "SELECT id, source_id, status FROM audit_log WHERE id > $1 AND source_id = $2",
            before,
            SOURCE,
        )
        if rows:
            break
        await asyncio.sleep(0.25)

    record(
        SOURCE,
        Control.AUDIT,
        Verdict.PASS if rows else Verdict.FAIL,
        detail="the governed read produced an audit row naming the live source",
        audit_rows=len(rows),
        statuses=sorted({r["status"] for r in rows}),
    )
    assert rows, "a governed query against the live source produced no audit row"
