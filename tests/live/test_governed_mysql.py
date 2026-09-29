"""Tier 1: governance applied to a real managed MySQL, over MCP.

MySQL is the counterpart to the PostgreSQL module and covers the protocol that
one cannot: `MySQLAdapter` consumes the `query` key the MCP path supplies, and
resolves `password_ref` through `_secret_value`, so the connector path works
where `ConnectionManager` does not.

The source connects as `generic_full_user`, which holds ALL PRIVILEGES on the
database. That is the deliberate choice described in `seed.py`: with the
upstream permitting everything, any refusal observed here is unambiguously
InterLock's. A restricted upstream user would make these tests unfalsifiable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tests.live.support import effects
from tests.live.support.config import LiveConfig, load_live_config
from tests.live.support.evidence import Control, Verdict, record
from tests.live.support.seed import AGENT_API_KEY, BLOCKED_API_KEY, source_id

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not load_live_config().has_mysql(),
        reason="live MySQL credentials are not configured",
    ),
]

SOURCE = source_id("mysql")
SSN = "123-45-6789"
EMAIL = "ada@example.com"


def _mcp(cfg: LiveConfig, api_key: str, sql: str) -> httpx.Response:
    return httpx.post(
        f"{cfg.e2e.gateway_url}/mcp/tools/call",
        json={"name": "interlock_query", "arguments": {"source_id": SOURCE, "sql": sql}},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60,
    )


@pytest.fixture(scope="module")
async def table(live_config: LiveConfig) -> AsyncIterator[str]:
    name = live_config.table_name
    await effects.mysql_execute(
        live_config,
        f"CREATE TABLE IF NOT EXISTS `{name}` "
        "(id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(64) NOT NULL, "
        "ssn VARCHAR(32), email VARCHAR(128))",
        user="generic_ddl_user",
    )
    await effects.mysql_execute(
        live_config,
        f"INSERT INTO `{name}` (label, ssn, email) VALUES ('alpha', %s, %s)",
        SSN,
        EMAIL,
        user="generic_full_user",
    )
    try:
        yield name
    finally:
        await effects.mysql_execute(
            live_config, f"DROP TABLE IF EXISTS `{name}`", user="generic_ddl_user"
        )


@pytest.mark.asyncio
async def test_a_granted_identity_can_read(live_config: LiveConfig, table: str) -> None:
    response = _mcp(live_config, AGENT_API_KEY, f"SELECT label FROM {table}")

    record(
        SOURCE,
        Control.ROLE_ALLOW,
        Verdict.PASS if "alpha" in response.text else Verdict.FAIL,
        detail="the granted identity read the certification table over MCP",
        status=response.status_code,
    )
    assert response.status_code == 200, response.text[:200]
    assert "alpha" in response.text, response.text[:200]


@pytest.mark.asyncio
async def test_a_blocked_identity_is_refused_for_the_right_reason(
    live_config: LiveConfig, table: str
) -> None:
    response = _mcp(live_config, BLOCKED_API_KEY, f"SELECT label FROM {table}")
    body = response.text.lower()

    governance_refusal = response.status_code == 403 and "denied" in body
    record(
        SOURCE,
        Control.ROLE_DENY,
        Verdict.PASS if governance_refusal else Verdict.FAIL,
        detail="the blocked identity was refused by governance, not by an incidental error",
        status=response.status_code,
        body=response.text[:160],
    )

    assert response.status_code == 403, f"expected a governance refusal, got {response.status_code}"
    assert "denied" in body, f"refused, but not by governance: {response.text[:200]}"
    assert "alpha" not in response.text, "the refusal still leaked row data"


@pytest.mark.asyncio
async def test_pii_is_redacted_although_the_upstream_still_holds_it(
    live_config: LiveConfig, table: str
) -> None:
    """Proven by contrast: redacted to the caller, intact at the source."""
    response = _mcp(live_config, AGENT_API_KEY, f"SELECT ssn, email FROM {table}")
    upstream = await effects.mysql_rows(
        live_config, f"SELECT ssn, email FROM `{table}`", user="generic_read_user"
    )

    served = response.text
    truth = " ".join(str(v) for row in upstream for v in row.values())

    redacted = SSN not in served and EMAIL not in served
    upstream_intact = SSN in truth and EMAIL in truth

    record(
        SOURCE,
        Control.REDACTION,
        Verdict.PASS if redacted and upstream_intact else Verdict.FAIL,
        detail=(
            "the gateway returned redaction markers over MCP while the upstream row "
            "still held the real values"
        ),
        served_contains_pii=not redacted,
        upstream_still_has_pii=upstream_intact,
    )

    assert upstream_intact, "precondition failed: the upstream row does not contain the PII"
    assert redacted, f"unredacted PII reached the caller: {served[:200]}"


@pytest.mark.asyncio
async def test_a_governed_write_reaches_the_upstream(live_config: LiveConfig, table: str) -> None:
    label = f"written-{live_config.run_id}"
    response = _mcp(live_config, AGENT_API_KEY, f"INSERT INTO {table} (label) VALUES ('{label}')")

    landed = await effects.mysql_rows(
        live_config,
        f"SELECT label FROM `{table}` WHERE label = %s",
        label,
        user="generic_read_user",
    )

    record(
        SOURCE,
        Control.WRITE_SAFETY,
        Verdict.PASS if len(landed) == 1 else Verdict.FAIL,
        detail="a governed write was confirmed present upstream by the independent reader",
        status=response.status_code,
        rows_found_upstream=len(landed),
    )
    assert len(landed) == 1, (
        f"the governed write never reached the upstream table "
        f"(gateway said {response.status_code}: {response.text[:160]})"
    )


@pytest.mark.asyncio
async def test_a_blocked_identitys_write_never_reaches_the_upstream(
    live_config: LiveConfig, table: str
) -> None:
    """The negative case, confirmed upstream rather than inferred from the refusal.

    The upstream user holds ALL PRIVILEGES, so nothing downstream of InterLock
    would have stopped this write. If a row appears, governance failed.
    """
    label = f"blocked-{live_config.run_id}"
    response = _mcp(live_config, BLOCKED_API_KEY, f"INSERT INTO {table} (label) VALUES ('{label}')")

    landed = await effects.mysql_rows(
        live_config,
        f"SELECT label FROM `{table}` WHERE label = %s",
        label,
        user="generic_read_user",
    )

    record(
        SOURCE,
        Control.APPROVAL_GATING,
        Verdict.PASS if not landed else Verdict.FAIL,
        detail=(
            "a refused write left no row upstream, despite the upstream user holding "
            "ALL PRIVILEGES - so only InterLock can have stopped it"
        ),
        status=response.status_code,
        rows_found_upstream=len(landed),
    )
    assert not landed, "a refused write still reached the upstream table"


@pytest.mark.asyncio
async def test_the_governed_query_is_audited(
    live_config: LiveConfig, table: str, control_plane: Any
) -> None:
    before = int(await control_plane.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    _mcp(live_config, AGENT_API_KEY, f"SELECT label FROM {table}")

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


@pytest.mark.asyncio
async def test_mysql_own_identifier_quoting_works(live_config: LiveConfig, table: str) -> None:
    """Backtick-quoted identifiers, which used to be unusable.

    Everything was parsed as PostgreSQL, so MySQL's own standard quoting
    failed to parse and fell to the conservative default: an unknown write at
    HIGH risk with no tables, which source roles refused because no role
    grants `db.execute_raw`. A plain SELECT was therefore refused as an
    unclassifiable write, and its audit row recorded operation=WRITE.

    Backticks are idiomatic MySQL, emitted by ORMs and by mysqldump, and
    required for reserved-word identifiers - so those were unreachable through
    the proxy entirely.

    Both quoting positions and both statement kinds are exercised, because the
    fix is in the grammar rather than in a special case for table names.
    """
    plain = _mcp(live_config, AGENT_API_KEY, f"SELECT label FROM {table}")
    backticked = _mcp(live_config, AGENT_API_KEY, f"SELECT label FROM `{table}`")
    backticked_column = _mcp(live_config, AGENT_API_KEY, f"SELECT `label` FROM `{table}`")

    write_label = f"backtick-{live_config.run_id}"
    backticked_write = _mcp(
        live_config,
        AGENT_API_KEY,
        f"INSERT INTO `{table}` (`label`) VALUES ('{write_label}')",
    )
    landed = await effects.mysql_rows(
        live_config,
        f"SELECT label FROM `{table}` WHERE label = %s",
        write_label,
        user="generic_read_user",
    )

    all_read = all(r.status_code == 200 for r in (plain, backticked, backticked_column))
    record(
        SOURCE,
        Control.ROLE_ALLOW,
        Verdict.PASS if all_read and len(landed) == 1 else Verdict.FAIL,
        detail=(
            "backtick-quoted identifiers are parsed in the source's own SQL grammar: "
            "reads succeed and a backticked INSERT is still classified as a write and "
            "reaches the upstream"
        ),
        plain_status=plain.status_code,
        backticked_status=backticked.status_code,
        backticked_column_status=backticked_column.status_code,
        backticked_write_status=backticked_write.status_code,
        rows_found_upstream=len(landed),
    )

    assert plain.status_code == 200, plain.text[:150]
    assert (
        backticked.status_code == 200
    ), f"a backticked SELECT was refused: {backticked.text[:200]}"
    assert backticked_column.status_code == 200, backticked_column.text[:150]
    assert len(landed) == 1, (
        "a backticked INSERT did not reach the upstream "
        f"(gateway said {backticked_write.status_code}: {backticked_write.text[:150]})"
    )
