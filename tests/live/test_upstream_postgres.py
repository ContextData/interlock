"""Tier 0: the live PostgreSQL upstream, before InterLock is involved.

This establishes the ground the governed tests stand on. If the upstream's own
grants are not what the credential file claims, every governance conclusion
drawn on top of it is unsafe: a query refused by the *database* would look
exactly like a query refused by InterLock, and the certification would report
a working control that was never exercised.

So the privilege matrix is asserted in both directions - each user can do what
it should, and cannot do what it should not - and the results feed the report
as `Control.UPSTREAM`. It replaces the equivalent checks in the retired
`tools/live_credentials_certify.py`, as a test that keeps running rather than
a script someone remembers to invoke.

Nothing here touches the gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests.live.support import effects
from tests.live.support.config import LiveConfig, load_live_config
from tests.live.support.evidence import Control, Verdict, record

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not load_live_config().has_postgres(),
        reason="live PostgreSQL credentials are not configured",
    ),
]

READ_USER = "generic_read_user"
WRITE_USER = "generic_write_user"
DDL_USER = "generic_ddl_user"


@pytest.fixture(scope="module")
async def cert_table(live_config: LiveConfig) -> AsyncIterator[str]:
    """A disposable, run-stamped table owned by the DDL user.

    Teardown runs even when the module fails, because a leaked table on a
    shared database is somebody's problem later.
    """
    table = live_config.table_name
    await effects.postgres_execute(
        live_config,
        f"CREATE TABLE IF NOT EXISTS {table} "
        "(id serial PRIMARY KEY, label text NOT NULL, ssn text, email text)",
        user=DDL_USER,
    )
    await effects.postgres_execute(
        live_config, f"GRANT SELECT ON {table} TO {READ_USER}", user=DDL_USER
    )
    await effects.postgres_execute(
        live_config,
        f"GRANT SELECT, INSERT, UPDATE ON {table} TO {WRITE_USER}",
        user=DDL_USER,
    )
    await effects.postgres_execute(
        live_config,
        f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {WRITE_USER}",
        user=DDL_USER,
    )
    try:
        yield table
    finally:
        await effects.postgres_execute(
            live_config, f"DROP TABLE IF EXISTS {table} CASCADE", user=DDL_USER
        )


async def _attempt(config: LiveConfig, user: str, statement: str) -> str | None:
    """Run a statement as a user; return None on success, else the SQLSTATE."""
    try:
        await effects.postgres_execute(config, statement, user=user)
    except asyncpg.PostgresError as exc:
        return getattr(exc, "sqlstate", "unknown")
    return None


@pytest.mark.asyncio
async def test_the_upstream_is_reachable_with_every_declared_identity(
    live_config: LiveConfig,
) -> None:
    """Each alias in the credential file must actually authenticate.

    A user that cannot connect would make every later refusal ambiguous.
    """
    reached = {}
    for user in (READ_USER, WRITE_USER, DDL_USER):
        rows = await effects.postgres_rows(live_config, "SELECT current_user AS who", user=user)
        reached[user] = rows[0]["who"]

    record(
        "postgresql",
        Control.UPSTREAM,
        Verdict.PASS,
        detail=f"all {len(reached)} declared users authenticated",
        authenticated_as=reached,
        host=live_config.pg_host,
        database=live_config.pg_database,
    )
    assert set(reached) == {READ_USER, WRITE_USER, DDL_USER}


@pytest.mark.asyncio
async def test_the_read_user_can_select_but_cannot_write_or_drop(
    live_config: LiveConfig, cert_table: str
) -> None:
    """The negative half is the half that matters.

    A read user that can insert makes any later 'InterLock blocked the write'
    conclusion unfalsifiable, because the write would have been blocked
    downstream regardless.
    """
    rows = await effects.postgres_rows(
        live_config, f"SELECT count(*) AS n FROM {cert_table}", user=READ_USER
    )
    assert rows[0]["n"] is not None, "read user could not select from the certification table"

    insert_state = await _attempt(
        live_config, READ_USER, f"INSERT INTO {cert_table} (label) VALUES ('nope')"
    )
    drop_state = await _attempt(live_config, READ_USER, f"DROP TABLE {cert_table}")

    record(
        "postgresql",
        Control.UPSTREAM,
        Verdict.PASS if insert_state and drop_state else Verdict.FAIL,
        detail="read user: select allowed, insert and drop refused by the database",
        select="allowed",
        insert_sqlstate=insert_state,
        drop_sqlstate=drop_state,
    )

    assert insert_state == "42501", f"read user was permitted to INSERT (sqlstate={insert_state})"
    assert drop_state is not None, "read user was permitted to DROP the table"
    # The table must still be there for the remaining tests.
    assert (
        await effects.postgres_rows(
            live_config, f"SELECT 1 AS ok FROM {cert_table} LIMIT 1", user=READ_USER
        )
        is not None
    )


@pytest.mark.asyncio
async def test_the_write_user_can_insert_and_update_but_cannot_drop(
    live_config: LiveConfig, cert_table: str
) -> None:
    await effects.postgres_execute(
        live_config,
        f"INSERT INTO {cert_table} (label, ssn, email) VALUES "
        "('write-user-probe', '123-45-6789', 'probe@example.com')",
        user=WRITE_USER,
    )
    await effects.postgres_execute(
        live_config,
        f"UPDATE {cert_table} SET label = 'write-user-updated' " "WHERE label = 'write-user-probe'",
        user=WRITE_USER,
    )

    # Read it back with the independent reader, not by trusting the statement.
    rows = await effects.postgres_rows(
        live_config,
        f"SELECT label FROM {cert_table} WHERE label = 'write-user-updated'",
        user=READ_USER,
    )
    drop_state = await _attempt(live_config, WRITE_USER, f"DROP TABLE {cert_table}")

    record(
        "postgresql",
        Control.UPSTREAM,
        Verdict.PASS if rows and drop_state else Verdict.FAIL,
        detail="write user: insert and update landed, drop refused by the database",
        rows_after_update=len(rows),
        drop_sqlstate=drop_state,
    )

    assert len(rows) == 1, "the write user's insert/update did not reach the table"
    assert drop_state is not None, "write user was permitted to DROP the table"


@pytest.mark.asyncio
async def test_the_ddl_user_can_create_and_drop(live_config: LiveConfig) -> None:
    scratch = f"{live_config.table_name}_ddl"
    await effects.postgres_execute(live_config, f"CREATE TABLE {scratch} (id int)", user=DDL_USER)
    exists = await effects.postgres_rows(
        live_config,
        "SELECT to_regclass($1) IS NOT NULL AS present",
        scratch,
        user=DDL_USER,
    )
    await effects.postgres_execute(live_config, f"DROP TABLE IF EXISTS {scratch}", user=DDL_USER)
    gone = await effects.postgres_rows(
        live_config,
        "SELECT to_regclass($1) IS NULL AS absent",
        scratch,
        user=DDL_USER,
    )

    record(
        "postgresql",
        Control.UPSTREAM,
        Verdict.PASS if exists[0]["present"] and gone[0]["absent"] else Verdict.FAIL,
        detail="ddl user: create and drop both took effect",
        created=bool(exists[0]["present"]),
        dropped=bool(gone[0]["absent"]),
    )

    assert exists[0]["present"], "ddl user could not create a table"
    assert gone[0]["absent"], "ddl user's drop did not take effect"
