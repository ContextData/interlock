"""Tier 0: the live MySQL upstream, before InterLock is involved.

Same purpose as the PostgreSQL module - establish that the database's own
grants match what the credential file claims, so that a refusal observed later
can be attributed to InterLock rather than to the upstream.

MySQL declares a fourth alias, `generic_full_user`, which the PostgreSQL side
does not have. It is certified for the one privilege that distinguishes it:
DELETE, which the write user must not hold.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.live.support import effects
from tests.live.support.config import LiveConfig, load_live_config
from tests.live.support.evidence import Control, Verdict, record

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not load_live_config().has_mysql(),
        reason="live MySQL credentials are not configured",
    ),
]

READ_USER = "generic_read_user"
WRITE_USER = "generic_write_user"
FULL_USER = "generic_full_user"
DDL_USER = "generic_ddl_user"


@pytest.fixture(scope="module")
async def cert_table(live_config: LiveConfig) -> AsyncIterator[str]:
    table = live_config.table_name
    await effects.mysql_execute(
        live_config,
        f"CREATE TABLE IF NOT EXISTS `{table}` "
        "(id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(64) NOT NULL, "
        "ssn VARCHAR(32), email VARCHAR(128))",
        user=DDL_USER,
    )
    try:
        yield table
    finally:
        await effects.mysql_execute(live_config, f"DROP TABLE IF EXISTS `{table}`", user=DDL_USER)


async def _attempt(config: LiveConfig, user: str, statement: str) -> str | None:
    """Run a statement as a user; return None on success, else the error class."""
    try:
        await effects.mysql_execute(config, statement, user=user)
    except Exception as exc:  # aiomysql raises several unrelated error types
        return type(exc).__name__
    return None


@pytest.mark.asyncio
async def test_the_upstream_is_reachable_with_every_declared_identity(
    live_config: LiveConfig,
) -> None:
    reached = {}
    for user in (READ_USER, WRITE_USER, FULL_USER, DDL_USER):
        rows = await effects.mysql_rows(live_config, "SELECT CURRENT_USER() AS who", user=user)
        reached[user] = str(rows[0]["who"]).split("@")[0]

    record(
        "mysql",
        Control.UPSTREAM,
        Verdict.PASS,
        detail=f"all {len(reached)} declared users authenticated",
        authenticated_as=reached,
        database=live_config.mysql_database,
    )
    assert set(reached) == {READ_USER, WRITE_USER, FULL_USER, DDL_USER}


@pytest.mark.asyncio
async def test_the_read_user_can_select_but_cannot_write(
    live_config: LiveConfig, cert_table: str
) -> None:
    rows = await effects.mysql_rows(
        live_config, f"SELECT COUNT(*) AS n FROM `{cert_table}`", user=READ_USER
    )
    assert rows[0]["n"] is not None, "read user could not select"

    insert_error = await _attempt(
        live_config, READ_USER, f"INSERT INTO `{cert_table}` (label) VALUES ('nope')"
    )
    drop_error = await _attempt(live_config, READ_USER, f"DROP TABLE `{cert_table}`")

    record(
        "mysql",
        Control.UPSTREAM,
        Verdict.PASS if insert_error and drop_error else Verdict.FAIL,
        detail="read user: select allowed, insert and drop refused by the database",
        insert_error=insert_error,
        drop_error=drop_error,
    )

    assert insert_error is not None, "read user was permitted to INSERT"
    assert drop_error is not None, "read user was permitted to DROP"


@pytest.mark.asyncio
async def test_the_write_user_can_insert_but_cannot_read_delete_or_filter(
    live_config: LiveConfig, cert_table: str
) -> None:
    """The write user holds INSERT and UPDATE, and no SELECT at all.

    That last part is not an accident of this test and is worth stating,
    because it shapes what the governed tests can ask for. Without SELECT,
    MySQL refuses `UPDATE ... WHERE` with error 1143 -- evaluating the
    predicate requires reading the column. So this user can append and can
    rewrite a whole column, but cannot target a row by its contents, and
    cannot read anything back.

    Established by running it, not assumed: the first version of this test
    asserted a conditional UPDATE would succeed, and the database disagreed.
    DELETE is what separates this alias from the full user; if the write user
    could delete, the two would be indistinguishable and any governance test
    resting on the difference would prove nothing.
    """
    await effects.mysql_execute(
        live_config,
        f"INSERT INTO `{cert_table}` (label, ssn, email) VALUES "
        "('write-probe', '123-45-6789', 'probe@example.com')",
        user=WRITE_USER,
    )

    select_error = await _attempt(
        live_config, WRITE_USER, f"SELECT label FROM `{cert_table}` LIMIT 1"
    )
    filtered_update_error = await _attempt(
        live_config,
        WRITE_USER,
        f"UPDATE `{cert_table}` SET label = 'x' WHERE label = 'write-probe'",
    )
    delete_error = await _attempt(
        live_config, WRITE_USER, f"DELETE FROM `{cert_table}` WHERE label = 'write-probe'"
    )

    # The insert is confirmed by the independent reader, never by the write
    # user itself -- which could not read it back even if we asked.
    rows = await effects.mysql_rows(
        live_config,
        f"SELECT label, ssn FROM `{cert_table}` WHERE label = 'write-probe'",
        user=READ_USER,
    )

    proven = bool(rows) and all((select_error, filtered_update_error, delete_error))
    record(
        "mysql",
        Control.UPSTREAM,
        Verdict.PASS if proven else Verdict.FAIL,
        detail=(
            "write user: insert landed; select, filtered update and delete all "
            "refused by the database"
        ),
        rows_after_insert=len(rows),
        select_error=select_error,
        filtered_update_error=filtered_update_error,
        delete_error=delete_error,
    )

    assert len(rows) == 1, "the write user's insert did not reach the table"
    assert select_error is not None, "write user was permitted to SELECT"
    assert filtered_update_error is not None, "write user was permitted a filtered UPDATE"
    assert delete_error is not None, "write user was permitted to DELETE"


@pytest.mark.asyncio
async def test_the_full_user_can_delete(live_config: LiveConfig, cert_table: str) -> None:
    await effects.mysql_execute(
        live_config,
        f"INSERT INTO `{cert_table}` (label) VALUES ('full-probe')",
        user=FULL_USER,
    )
    await effects.mysql_execute(
        live_config, f"DELETE FROM `{cert_table}` WHERE label = 'full-probe'", user=FULL_USER
    )
    remaining = await effects.mysql_rows(
        live_config,
        f"SELECT COUNT(*) AS n FROM `{cert_table}` WHERE label = 'full-probe'",
        user=READ_USER,
    )

    record(
        "mysql",
        Control.UPSTREAM,
        Verdict.PASS if remaining[0]["n"] == 0 else Verdict.FAIL,
        detail="full user: delete took effect, confirmed by the independent reader",
        rows_remaining=int(remaining[0]["n"]),
    )

    assert remaining[0]["n"] == 0, "the full user's delete did not take effect"


@pytest.mark.asyncio
async def test_the_ddl_user_can_create_and_drop(live_config: LiveConfig) -> None:
    scratch = f"{live_config.table_name}_ddl"
    # The managed instance runs with sql_require_primary_key=ON, so a table
    # without one is refused outright. Learned from error 3750 on the first
    # run; a local MySQL would have accepted the bare `(id INT)` and hidden
    # the difference between this environment and a developer's.
    await effects.mysql_execute(
        live_config, f"CREATE TABLE `{scratch}` (id INT PRIMARY KEY)", user=DDL_USER
    )
    present = await effects.mysql_rows(
        live_config,
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        live_config.mysql_database,
        scratch,
        user=DDL_USER,
    )
    await effects.mysql_execute(live_config, f"DROP TABLE IF EXISTS `{scratch}`", user=DDL_USER)
    absent = await effects.mysql_rows(
        live_config,
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        live_config.mysql_database,
        scratch,
        user=DDL_USER,
    )

    record(
        "mysql",
        Control.UPSTREAM,
        Verdict.PASS if present[0]["n"] == 1 and absent[0]["n"] == 0 else Verdict.FAIL,
        detail="ddl user: create and drop both took effect",
        after_create=int(present[0]["n"]),
        after_drop=int(absent[0]["n"]),
    )

    assert present[0]["n"] == 1, "ddl user could not create a table"
    assert absent[0]["n"] == 0, "ddl user's drop did not take effect"
