"""SQL governance gaps, pinned before they are fixed.

Each gap here was found while planning the source catalog and is written as the
behaviour InterLock *should* have. The multi-statement, ``set_config`` and
``table_to_xml`` gaps are fixed and these are now their regression tests; the
rest are still marked ``xfail(strict=True)``. Today the test
fails, which is the evidence; when the fix lands the test passes, and ``strict``
turns that unexpected pass into a failure so the fix has to remove the marker
rather than leave a stale one behind.

Every bypass test has a separate baseline test, not marked, proving the control
works in the simple case. Without that split a broken baseline would hide inside
an "expected failure" and the characterisation would prove nothing.

The headline gap, reproduced by hand before this file was written: with a deny
policy on ``orders``, ``SELECT * FROM orders`` is refused, but
``SELECT 1; SELECT * FROM orders`` over the PostgreSQL wire is allowed and the
upstream answers ``SELECT 2`` - two rows read from the denied table. The gateway
parses a multi-statement query, keeps only the first statement as request
metadata, and evaluates governance against no tables at all.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import asyncpg
import pytest

from interlock.core.auth import AuthManager
from interlock.core.normalizer import normalize_sql
from tests.e2e.support.clients import mcp_call

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("catalogued_sql_sources")]

_SEED_TAG = "sql_bypass_char"
_POLICY_LABEL = "sql_bypass_char_agent"
_DENY_ORDERS_POLICY = "sql-bypass-char-deny-orders"


# --------------------------------------------------------------------------
# Seeding helpers - self-contained so a change to the shared seed cannot turn
# one of these into a test of something else.
# --------------------------------------------------------------------------


async def _notify_policy(conn: Any) -> None:
    await conn.execute("SELECT pg_notify('onyx_policy_changed', $1)", _SEED_TAG)
    await asyncio.sleep(0.5)


async def _role_with(
    conn: Any, *, source_id: str, role_key: str, permissions: list[dict[str, Any]]
) -> int:
    role_id = await conn.fetchval(
        """
        INSERT INTO source_roles (source_id, role_key, name, description, metadata)
        VALUES ($1, $2, $2, 'sql bypass characterisation', $3::jsonb)
        ON CONFLICT (source_id, role_key) DO UPDATE
        SET enabled = TRUE, updated_at = NOW()
        RETURNING id
        """,
        source_id,
        role_key,
        json.dumps({"seed": _SEED_TAG}),
    )
    await conn.execute("DELETE FROM source_role_permissions WHERE role_id = $1", role_id)
    for permission in permissions:
        await conn.execute(
            """
            INSERT INTO source_role_permissions
                (role_id, effect, action, resource_type, resource_pattern, constraints)
            VALUES ($1, $2, $3, $4, $5, '{}'::jsonb)
            """,
            role_id,
            permission["effect"],
            permission["action"],
            permission["resource_type"],
            permission["resource_pattern"],
        )
    return int(role_id)


async def _identity(conn: Any, *, source_id: str, role_id: int) -> str:
    """An identity whose only authority is one role on one source; returns its key."""
    api_key = f"sqlbypass-{secrets.token_urlsafe(24)}"
    identity_id = await conn.fetchval(
        """
        INSERT INTO identities (name, api_key_hash, agent_type, team, roles, enabled, metadata)
        VALUES ($1, $2, 'custom', 'e2e', $3::text[], TRUE, $4::jsonb)
        RETURNING id
        """,
        f"sql-bypass-{secrets.token_hex(4)}",
        AuthManager.hash_api_key(api_key),
        [_POLICY_LABEL],
        json.dumps({"seed": _SEED_TAG}),
    )
    await conn.execute(
        """
        INSERT INTO identity_source_role_grants (identity_id, source_id, role_id, metadata)
        VALUES ($1, $2, $3, $4::jsonb)
        """,
        identity_id,
        source_id,
        role_id,
        json.dumps({"seed": _SEED_TAG}),
    )
    return api_key


async def _allow_policy(conn: Any, source_id: str) -> None:
    """Policy default-denies anything no rule matches, so these identities need
    their own allow rule to get past policy and reach the source-role decision.

    ``identity_roles``, not ``roles``: ``roles`` matches source-role keys, while
    the label written into ``identities.roles`` is matched by ``identity_roles``.
    """
    await conn.execute(
        """
        INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
        VALUES ($1, 5, $2::jsonb, '{"effect": "allow"}'::jsonb, TRUE)
        """,
        f"{_SEED_TAG}-{source_id}",
        json.dumps({"source_ids": [source_id], "identity_roles": [_POLICY_LABEL]}),
    )


async def _cleanup(conn: Any) -> None:
    await conn.execute(
        "DELETE FROM policy_rules WHERE name LIKE $1 OR name = $2",
        f"{_SEED_TAG}%",
        _DENY_ORDERS_POLICY,
    )
    await conn.execute(
        "DELETE FROM identity_source_role_grants WHERE metadata->>'seed' = $1", _SEED_TAG
    )
    await conn.execute(
        "DELETE FROM source_role_permissions WHERE role_id IN"
        " (SELECT id FROM source_roles WHERE metadata->>'seed' = $1)",
        _SEED_TAG,
    )
    await conn.execute("DELETE FROM source_roles WHERE metadata->>'seed' = $1", _SEED_TAG)
    await conn.execute("DELETE FROM identities WHERE metadata->>'seed' = $1", _SEED_TAG)
    await _notify_policy(conn)


def _pg_params(e2e_config: Any, api_key: str | None = None) -> dict[str, Any]:
    params = dict(e2e_config.gateway_pg_params_api_key)
    if api_key is not None:
        params["password"] = api_key
    return params


async def _pg_refuses(e2e_config: Any, sql: str, *, api_key: str | None = None) -> bool:
    """True when the gateway refuses the statement over the PostgreSQL wire.

    ``execute`` with no arguments uses the simple-query protocol, which is the
    one that carries several statements in one message.
    """
    conn = await asyncpg.connect(**_pg_params(e2e_config, api_key))
    try:
        await conn.execute(sql)
    except asyncpg.PostgresError:
        return True
    finally:
        await conn.close()
    return False


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
async def deny_orders_policy(control_db: Any, e2e_config: Any) -> Any:
    """A deny policy on `orders` for the seeded agent, above its allow rule."""
    await _cleanup(control_db)
    await control_db.execute(
        """
        INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
        VALUES ($1, 200, $2::jsonb, '{"effect": "deny"}'::jsonb, TRUE)
        """,
        _DENY_ORDERS_POLICY,
        json.dumps({"source_ids": [e2e_config.source_id_pg], "tables": ["orders"]}),
    )
    await _notify_policy(control_db)
    try:
        yield control_db
    finally:
        await _cleanup(control_db)


@pytest.fixture
async def role_deny_orders_key(control_db: Any, e2e_config: Any) -> Any:
    """An identity allowed every table but explicitly denied `public.orders`."""
    await _cleanup(control_db)
    role_id = await _role_with(
        control_db,
        source_id=e2e_config.source_id_pg,
        role_key="sql_bypass_char_orders_denied",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "*.*",
            },
            {
                "effect": "deny",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "public.orders.*",
            },
        ],
    )
    api_key = await _identity(control_db, source_id=e2e_config.source_id_pg, role_id=role_id)
    await _allow_policy(control_db, e2e_config.source_id_pg)
    await _notify_policy(control_db)
    try:
        yield api_key
    finally:
        await _cleanup(control_db)


@pytest.fixture
async def column_deny_key(control_db: Any, e2e_config: Any) -> Any:
    """An identity allowed every table but explicitly denied `customers.ssn`."""
    await _cleanup(control_db)
    role_id = await _role_with(
        control_db,
        source_id=e2e_config.source_id_pg,
        role_key="sql_bypass_char_ssn_denied",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "*.*",
            },
            {
                "effect": "deny",
                "action": "db.table.select",
                "resource_type": "db.column",
                "resource_pattern": "public.customers.ssn",
            },
        ],
    )
    api_key = await _identity(control_db, source_id=e2e_config.source_id_pg, role_id=role_id)
    await _allow_policy(control_db, e2e_config.source_id_pg)
    await _notify_policy(control_db)
    try:
        yield api_key
    finally:
        await _cleanup(control_db)


# --------------------------------------------------------------------------
# Multi-statement input on the PostgreSQL wire
# --------------------------------------------------------------------------


async def test_baseline_a_deny_policy_refuses_a_single_statement(
    deny_orders_policy: Any, e2e_config: Any
) -> None:
    assert await _pg_refuses(e2e_config, "SELECT * FROM orders")


async def test_a_deny_policy_refuses_the_denied_table_after_an_innocuous_statement(
    deny_orders_policy: Any, e2e_config: Any
) -> None:
    assert await _pg_refuses(e2e_config, "SELECT 1; SELECT * FROM orders")


async def test_a_deny_policy_on_a_bare_name_refuses_the_qualified_spelling(
    deny_orders_policy: Any, e2e_config: Any
) -> None:
    """The policy names `orders`; the query names `public.orders`. Policy tables
    used to be compared as written, so the qualified spelling walked past the
    deny. Deny rules now match every spelling of the table."""
    assert await _pg_refuses(e2e_config, "SELECT * FROM public.orders")


async def test_baseline_a_source_role_deny_refuses_a_single_statement(
    role_deny_orders_key: str, e2e_config: Any
) -> None:
    assert await _pg_refuses(e2e_config, "SELECT * FROM orders", api_key=role_deny_orders_key)
    assert not await _pg_refuses(
        e2e_config, "SELECT * FROM customers", api_key=role_deny_orders_key
    )


async def test_a_source_role_deny_refuses_the_denied_table_after_an_innocuous_statement(
    role_deny_orders_key: str, e2e_config: Any
) -> None:
    assert await _pg_refuses(
        e2e_config, "SELECT 1; SELECT * FROM orders", api_key=role_deny_orders_key
    )


async def test_mcp_does_not_return_a_denied_table_behind_an_innocuous_statement(
    deny_orders_policy: Any, e2e_config: Any
) -> None:
    """MCP also keeps only the first statement's tables, but asyncpg's extended
    protocol refuses several commands in one call, so nothing is returned. This
    pins that containment so it cannot quietly go away."""
    resp = mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": "SELECT 1; SELECT * FROM orders"},
    )
    assert resp.status_code != 200 or resp.json().get("isError", False), resp.text


# --------------------------------------------------------------------------
# Functions that reach data or session state without a parseable table
# --------------------------------------------------------------------------


async def test_baseline_plain_set_is_not_treated_as_a_read(e2e_config: Any) -> None:
    assert await _pg_refuses(e2e_config, "SET search_path TO public")


async def test_set_config_is_not_treated_as_a_read(e2e_config: Any) -> None:
    """``set_config('search_path', ...)`` changes how every later bare table name
    in a persistent PG-wire session resolves, which is exactly what a plain
    ``SET`` is refused for."""
    assert await _pg_refuses(e2e_config, "SELECT set_config('search_path', 'public', false)")


async def test_table_to_xml_cannot_read_a_denied_table(
    deny_orders_policy: Any, e2e_config: Any
) -> None:
    assert await _pg_refuses(e2e_config, "SELECT table_to_xml('public.orders', true, false, '')")


# --------------------------------------------------------------------------
# Column-level rules
# --------------------------------------------------------------------------


async def test_baseline_an_undenied_column_is_readable(
    column_deny_key: str, e2e_config: Any
) -> None:
    resp = mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": "SELECT name FROM customers"},
        headers={"Authorization": f"Bearer {column_deny_key}"},
    )
    assert resp.status_code == 200, resp.text


async def test_a_column_deny_refuses_that_column(column_deny_key: str, e2e_config: Any) -> None:
    """The request resource is ``public.customers.*`` whatever the SELECT list,
    and the deny pattern ``public.customers.ssn`` never matches it - so the deny
    statement is accepted by the role editor and then silently never fires."""
    resp = mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": "SELECT ssn FROM customers"},
        headers={"Authorization": f"Bearer {column_deny_key}"},
    )
    assert resp.status_code in (401, 403), resp.text


# --------------------------------------------------------------------------
# Name qualification
# --------------------------------------------------------------------------


async def test_a_mysql_table_is_qualified_with_its_own_database(
    control_db: Any, e2e_config: Any
) -> None:
    """MySQL has no ``public`` schema. A role written against the real database
    name - which is what the catalog pickers will generate - never matches the
    resource governance actually evaluates."""
    marker = f"/* {secrets.token_hex(6)} */"
    resp = mcp_call(
        e2e_config,
        "interlock_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": f"SELECT name FROM customers WHERE id = 1 {marker}",
        },
    )
    assert resp.status_code == 200, resp.text

    # The gateway's audit buffer flushes in the background.
    row = None
    for _ in range(40):
        row = await control_db.fetchrow(
            """
            SELECT request_metadata FROM audit_log
            WHERE source_id = $1 AND created_at > NOW() - INTERVAL '1 minute'
            ORDER BY created_at DESC LIMIT 1
            """,
            e2e_config.source_id_mysql,
        )
        if row is not None:
            break
        await asyncio.sleep(0.25)
    assert row is not None, "the gateway never audited the request"
    metadata = row["request_metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    resources = metadata.get("permission_request", {}).get("resources", [])
    assert resources, metadata
    assert not any(str(r).startswith("public.") for r in resources), resources


def test_baseline_a_two_part_snowflake_name_keeps_its_schema() -> None:
    normalized = normalize_sql("SELECT * FROM sch.t", "sf", dialect="snowflake")
    assert not isinstance(normalized, list)
    assert [name.lower() for name in normalized.tables] == ["sch.t"]


def test_a_three_part_snowflake_name_keeps_its_database() -> None:
    """``otherdb.sch.t`` collapses to ``sch.t``, so a role scoped to ``sch.*`` in
    the configured database would also match a same-named schema in another
    database."""
    normalized = normalize_sql("SELECT * FROM otherdb.sch.t", "sf", dialect="snowflake")
    assert not isinstance(normalized, list)
    assert [name.lower() for name in normalized.tables] == ["otherdb.sch.t"]


# --------------------------------------------------------------------------
# Column-level rules on the live paths (PR 4)
# --------------------------------------------------------------------------


@pytest.fixture
async def column_allow_key(control_db: Any, e2e_config: Any) -> Any:
    """An identity that may read only `customers.id` and `customers.name`."""
    await _cleanup(control_db)
    role_id = await _role_with(
        control_db,
        source_id=e2e_config.source_id_pg,
        role_key="sql_bypass_char_two_columns",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.column",
                "resource_pattern": "public.customers.id",
            },
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.column",
                "resource_pattern": "public.customers.name",
            },
        ],
    )
    api_key = await _identity(control_db, source_id=e2e_config.source_id_pg, role_id=role_id)
    await _allow_policy(control_db, e2e_config.source_id_pg)
    await _notify_policy(control_db)
    try:
        yield api_key
    finally:
        await _cleanup(control_db)


def _mcp_as(e2e_config: Any, api_key: str, sql: str) -> Any:
    return mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": sql},
        headers={"Authorization": f"Bearer {api_key}"},
    )


async def test_a_column_deny_refuses_that_column_over_the_pg_wire(
    column_deny_key: str, e2e_config: Any
) -> None:
    assert await _pg_refuses(e2e_config, "SELECT ssn FROM customers", api_key=column_deny_key)
    assert not await _pg_refuses(e2e_config, "SELECT name FROM customers", api_key=column_deny_key)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",
        "SELECT name FROM customers WHERE ssn = '000-00-0000'",
        "SELECT c.name FROM customers c ORDER BY c.ssn",
        "SELECT x FROM (SELECT ssn AS x FROM customers) s",
    ],
    ids=["star", "where", "order-by", "subquery"],
)
async def test_a_column_deny_covers_every_way_of_reading_the_column(
    sql: str, column_deny_key: str, e2e_config: Any
) -> None:
    resp = _mcp_as(e2e_config, column_deny_key, sql)
    assert resp.status_code in (401, 403), resp.text


async def test_a_whole_row_reference_fails_closed_under_a_column_deny(
    column_deny_key: str, e2e_config: Any
) -> None:
    resp = _mcp_as(e2e_config, column_deny_key, "SELECT to_jsonb(c) FROM customers c")
    assert resp.status_code in (401, 403), resp.text
    assert "could not be determined" in resp.text


async def test_a_column_allow_reads_exactly_those_columns(
    column_allow_key: str, e2e_config: Any
) -> None:
    allowed = _mcp_as(e2e_config, column_allow_key, "SELECT name FROM customers WHERE id = 1")
    assert allowed.status_code == 200, allowed.text
    for sql in (
        "SELECT * FROM customers",
        "SELECT name FROM customers WHERE email LIKE '%'",
        "SELECT count(*) FROM customers",
    ):
        refused = _mcp_as(e2e_config, column_allow_key, sql)
        assert refused.status_code in (401, 403), (sql, refused.text)


@pytest.fixture
async def redact_ssn_key(control_db: Any, e2e_config: Any) -> Any:
    """An identity allowed every table, under a policy that redacts `ssn`."""
    await _cleanup(control_db)
    role_id = await _role_with(
        control_db,
        source_id=e2e_config.source_id_pg,
        role_key="sql_bypass_char_redact_ssn",
        permissions=[
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "*.*",
            }
        ],
    )
    api_key = await _identity(control_db, source_id=e2e_config.source_id_pg, role_id=role_id)
    await control_db.execute(
        """
        INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
        VALUES ($1, 6, $2::jsonb, $3::jsonb, TRUE)
        """,
        f"{_SEED_TAG}-redact-ssn",
        json.dumps({"source_ids": [e2e_config.source_id_pg], "identity_roles": [_POLICY_LABEL]}),
        json.dumps({"effect": "allow", "redact_columns": ["customers.ssn"]}),
    )
    await _notify_policy(control_db)
    try:
        yield api_key
    finally:
        await _cleanup(control_db)


async def test_redact_columns_follows_a_renamed_column_over_mcp(
    redact_ssn_key: str, e2e_config: Any
) -> None:
    resp = _mcp_as(
        e2e_config,
        redact_ssn_key,
        "SELECT ssn AS tax_id, upper(ssn) AS loud, name FROM customers ORDER BY id LIMIT 1",
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert body.count("[REDACTED:POLICY]") >= 2, body
    assert "tax_id" in body and "loud" in body
