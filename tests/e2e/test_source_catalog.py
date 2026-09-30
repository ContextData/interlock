"""The source catalog, end to end.

Scans run in the compose worker containers - the only place that can reach the
compose sources by name - so these tests queue work and wait for a real worker
to finish it. The store's rules are also exercised directly against the control
database, where a snapshot can be shaped exactly.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import asyncpg
import pytest

from interlock.catalog.model import CatalogNode, CatalogSnapshot
from interlock.catalog.queue import enqueue_catalog_scan
from interlock.catalog.store import apply_snapshot

pytestmark = [pytest.mark.e2e]

_SCAN_TIMEOUT_SECONDS = 60


async def _wait_for_scan(control_db: Any, source_id: str, *, trigger: str | None = None) -> Any:
    """Wait for the newest scan of `source_id` to finish, and return it."""
    deadline = asyncio.get_running_loop().time() + _SCAN_TIMEOUT_SECONDS
    row = None
    while asyncio.get_running_loop().time() < deadline:
        row = await control_db.fetchrow(
            """
            SELECT * FROM source_catalog_scans
            WHERE source_id = $1 AND ($2::text IS NULL OR trigger = $2)
            ORDER BY id DESC LIMIT 1
            """,
            source_id,
            trigger,
        )
        if row is not None and row["status"] in ("succeeded", "failed", "cancelled"):
            return row
        await asyncio.sleep(1)
    raise AssertionError(f"no finished scan for {source_id} within the timeout: {row}")


async def _wait_for_scan_id(control_db: Any, scan_id: int) -> Any:
    deadline = asyncio.get_running_loop().time() + _SCAN_TIMEOUT_SECONDS
    row = None
    while asyncio.get_running_loop().time() < deadline:
        row = await control_db.fetchrow("SELECT * FROM source_catalog_scans WHERE id = $1", scan_id)
        if row is not None and row["status"] in ("succeeded", "failed", "cancelled"):
            return row
        await asyncio.sleep(1)
    raise AssertionError(f"scan {scan_id} did not finish within the timeout: {row}")


async def _scan_now(control_db: Any, source_id: str) -> Any:
    """Queue a manual scan and wait for it.

    The workers' schedulers may have queued a scan of this source already; the
    request then joins that scan and brings it forward, which is what waiting on
    the returned id relies on.
    """
    scan_id = await enqueue_catalog_scan(control_db, source_id, trigger="manual")
    assert scan_id is not None
    return await _wait_for_scan_id(control_db, scan_id)


async def _live_nodes(control_db: Any, source_id: str) -> dict[tuple[str, ...], Any]:
    rows = await control_db.fetch(
        "SELECT * FROM source_catalog WHERE source_id = $1 AND removed_at IS NULL", source_id
    )
    return {tuple(row["path"]): row for row in rows}


def _pg_form(e2e_config: Any, source_id: str) -> dict[str, str]:
    return {
        "source_id": source_id,
        "name": "E2E catalog source",
        "source_type": "postgresql",
        "connector_key": "postgresql",
        "cache_strategy": "bypass",
        "host": e2e_config.compose_source_pg_host,
        "port": "5432",
        "database": e2e_config.source_pg_database,
        "user": e2e_config.source_pg_user,
        "sslmode": "disable",
    }


def _pg_config(e2e_config: Any) -> dict[str, Any]:
    return {
        # The compose host is on a private network, as the seed's sources are.
        "allow_private_egress": True,
        "host": e2e_config.compose_source_pg_host,
        "port": 5432,
        "database": e2e_config.source_pg_database,
        "user": e2e_config.source_pg_user,
        "password": e2e_config.source_pg_password,
        "sslmode": "disable",
    }


async def _insert_source(
    control_db: Any, e2e_config: Any, source_id: str, *, enabled: bool
) -> None:
    await control_db.execute(
        """
        INSERT INTO data_sources (source_id, name, source_type, connection_config,
                                  cache_strategy, enabled, metadata)
        VALUES ($1, 'E2E catalog source', 'postgresql', $2::jsonb, 'bypass', $3, $4::jsonb)
        """,
        source_id,
        _pg_config(e2e_config),
        enabled,
        {"connector_key": "postgresql"},
    )
    await control_db.execute("NOTIFY onyx_config_changed")


async def _drop_source(control_db: Any, source_id: str) -> None:
    await control_db.execute("DELETE FROM data_sources WHERE source_id = $1", source_id)
    await control_db.execute("NOTIFY onyx_config_changed")


@pytest.fixture
async def catalog_db(e2e_config: Any) -> Any:
    """A control connection with the pool's JSONB codec, like the product uses."""
    conn = await asyncpg.connect(e2e_config.control_dsn)
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    try:
        yield conn
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Scans of the seeded SQL sources
# --------------------------------------------------------------------------


async def test_a_scan_catalogues_postgres_schemas_tables_and_columns(
    catalog_db: Any, e2e_config: Any
) -> None:
    scan = await _scan_now(catalog_db, e2e_config.source_id_pg)
    assert scan["status"] == "succeeded", dict(scan)
    assert scan["truncated"] is False

    nodes = await _live_nodes(catalog_db, e2e_config.source_id_pg)
    assert ("public", "customers") in nodes
    email = nodes[("public", "customers", "email")]
    assert email["node_type"] == "column"
    assert email["resource_key"] == "public.customers.email"
    assert email["data_type"]
    root = nodes[()]
    assert root["attributes"]["default_schema"] == "public"
    assert root["attributes"]["login"] == e2e_config.source_pg_user


async def test_a_scan_catalogues_mysql_with_its_own_database_as_default(
    catalog_db: Any, e2e_config: Any
) -> None:
    scan = await _scan_now(catalog_db, e2e_config.source_id_mysql)
    assert scan["status"] == "succeeded", dict(scan)
    nodes = await _live_nodes(catalog_db, e2e_config.source_id_mysql)
    root = nodes[()]
    assert root["attributes"]["dialect"] == "mysql"
    default_schema = root["attributes"]["default_schema"]
    assert default_schema and default_schema != "public"
    assert (default_schema, "customers") in nodes


async def test_no_scan_ever_stores_row_data(catalog_db: Any, e2e_config: Any) -> None:
    """Structure only: a seeded customer's email must not appear anywhere."""
    await _scan_now(catalog_db, e2e_config.source_id_pg)
    source = await asyncpg.connect(e2e_config.source_dsn)
    try:
        email = await source.fetchval("SELECT email FROM customers ORDER BY id LIMIT 1")
    finally:
        await source.close()
    leaked = await catalog_db.fetchval(
        "SELECT count(*) FROM source_catalog WHERE source_id = $1 AND attributes::text LIKE $2",
        e2e_config.source_id_pg,
        f"%{email}%",
    )
    assert leaked == 0


# --------------------------------------------------------------------------
# Drift against a real upstream
# --------------------------------------------------------------------------


async def test_drift_is_recorded_as_tables_and_columns_come_and_go(
    catalog_db: Any, e2e_config: Any
) -> None:
    source_id = f"e2e_catalog_drift_{secrets.token_hex(4)}"
    table = f"catalog_drift_{secrets.token_hex(4)}"
    upstream = await asyncpg.connect(e2e_config.source_dsn)
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    try:
        baseline = await _scan_now(catalog_db, source_id)
        assert baseline["status"] == "succeeded" and baseline["baseline"] is True
        assert (
            await catalog_db.fetchval(
                "SELECT count(*) FROM source_catalog_changes WHERE scan_id = $1", baseline["id"]
            )
            == 0
        ), "a baseline scan must not write change rows"

        await upstream.execute(f"CREATE TABLE {table} (id int)")
        added = await _scan_now(catalog_db, source_id)
        assert added["baseline"] is False
        changes = {
            (row["change"], tuple(row["path"]))
            for row in await catalog_db.fetch(
                "SELECT change, path FROM source_catalog_changes WHERE scan_id = $1", added["id"]
            )
        }
        assert ("added", ("public", table)) in changes
        assert ("added", ("public", table, "id")) in changes

        await upstream.execute(f"ALTER TABLE {table} ADD COLUMN note text")
        widened = await _scan_now(catalog_db, source_id)
        widened_changes = await catalog_db.fetch(
            "SELECT change, path FROM source_catalog_changes WHERE scan_id = $1", widened["id"]
        )
        assert ("added", ("public", table, "note")) in {
            (r["change"], tuple(r["path"])) for r in widened_changes
        }

        await upstream.execute(f"DROP TABLE {table}")
        dropped = await _scan_now(catalog_db, source_id)
        assert dropped["removed_count"] >= 3
        still_live = await _live_nodes(catalog_db, source_id)
        assert ("public", table) not in still_live
        remembered = await catalog_db.fetchval(
            "SELECT removed_at IS NOT NULL FROM source_catalog WHERE source_id = $1 AND path = $2",
            source_id,
            ["public", table],
        )
        assert remembered is True, "a removed table is kept and marked, not forgotten"
    finally:
        await upstream.execute(f"DROP TABLE IF EXISTS {table}")
        await upstream.close()
        await _drop_source(catalog_db, source_id)


# --------------------------------------------------------------------------
# Every save path queues a scan the worker completes
# --------------------------------------------------------------------------


async def _api_create(admin: Any, e2e_config: Any, catalog_db: Any, source_id: str) -> None:
    response = admin.client.post(
        "/api/data-sources",
        json={
            "source_id": source_id,
            "name": "E2E catalog source",
            "source_type": "postgresql",
            "connection_config": _pg_config(e2e_config),
            "cache_strategy": "bypass",
        },
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    assert response.status_code == 201, response.text


async def _api_update(admin: Any, e2e_config: Any, catalog_db: Any, source_id: str) -> None:
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    response = admin.client.put(
        f"/api/data-sources/{source_id}",
        json={"name": "E2E catalog source, renamed"},
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    assert response.status_code == 200, response.text


async def _console_create(admin: Any, e2e_config: Any, catalog_db: Any, source_id: str) -> None:
    response = admin.client.post(
        "/dashboard/data-sources/create",
        data=_pg_form(e2e_config, source_id),
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    assert response.status_code == 200, response.text[:300]


def _private_pg_form(e2e_config: Any, source_id: str) -> dict[str, str]:
    """The console form with the private-network option ticked and a password."""
    return {
        **_pg_form(e2e_config, source_id),
        "password": e2e_config.source_pg_password,
        "allow_private_egress": "on",
    }


async def _console_create_private(
    admin: Any, e2e_config: Any, catalog_db: Any, source_id: str
) -> None:
    response = admin.client.post(
        "/dashboard/data-sources/create",
        data=_private_pg_form(e2e_config, source_id),
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    assert response.status_code == 200, response.text[:300]


async def _wizard_save_private(
    admin: Any, e2e_config: Any, catalog_db: Any, source_id: str
) -> None:
    response = admin.client.post(
        "/dashboard/source-wizard/save",
        data=_private_pg_form(e2e_config, source_id),
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    assert response.status_code == 303, response.text[:300]


async def _console_edit(admin: Any, e2e_config: Any, catalog_db: Any, source_id: str) -> None:
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    response = admin.client.post(
        f"/dashboard/data-sources/{source_id}/edit",
        data={"name": "E2E catalog source, edited", "cache_strategy": "bypass", "enabled": "on"},
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    assert response.status_code in (200, 303), response.text[:300]


async def _console_enable(admin: Any, e2e_config: Any, catalog_db: Any, source_id: str) -> None:
    await _insert_source(catalog_db, e2e_config, source_id, enabled=False)
    response = admin.client.post(
        f"/dashboard/data-sources/{source_id}/toggle-enabled",
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    assert response.status_code in (200, 303), response.text[:300]


async def _wizard_save(admin: Any, e2e_config: Any, catalog_db: Any, source_id: str) -> None:
    response = admin.client.post(
        "/dashboard/source-wizard/save",
        data=_pg_form(e2e_config, source_id),
        headers={"X-CSRF-Token": admin.csrf_token},
    )
    # A successful save redirects to the new source's page.
    assert response.status_code == 303, response.text[:300]
    assert response.headers["location"] == f"/dashboard/data-sources/{source_id}"


@pytest.mark.parametrize(
    ("save", "outcome"),
    [
        (_api_create, "succeeded"),
        (_api_update, "succeeded"),
        # Without the private-network option, a source the console or wizard
        # creates for a private host is refused when scanned - the egress
        # guard working, recorded with a reason an admin can act on. With it
        # ticked, the same paths reach the host.
        (_console_create, "egress_blocked"),
        (_console_create_private, "succeeded"),
        (_console_edit, "succeeded"),
        (_console_enable, "succeeded"),
        (_wizard_save, "egress_blocked"),
        (_wizard_save_private, "succeeded"),
    ],
    ids=[
        "api-create",
        "api-update",
        "console-create",
        "console-create-private",
        "console-edit",
        "enable",
        "wizard",
        "wizard-private",
    ],
)
async def test_every_save_path_queues_a_scan(
    save: Any, outcome: str, admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    source_id = f"e2e_catalog_save_{secrets.token_hex(4)}"
    try:
        await save(admin_session, e2e_config, catalog_db, source_id)
        scan = await _wait_for_scan(catalog_db, source_id, trigger="save")
        assert scan["requested_by"] == e2e_config.admin_username
        if outcome == "succeeded":
            assert scan["status"] == "succeeded", dict(scan)
            assert ("public", "customers") in await _live_nodes(catalog_db, source_id)
        else:
            assert (scan["status"], scan["error_code"]) == ("failed", outcome), dict(scan)
    finally:
        await _drop_source(catalog_db, source_id)


async def test_the_scan_api_queues_and_lists(admin_session: Any, e2e_config: Any) -> None:
    queued = admin_session.client.post(
        f"/api/data-sources/{e2e_config.source_id_pg}/catalog/scans",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["trigger"] in ("api", "save", "manual", "scheduled", "startup")
    listed = admin_session.client.get(f"/api/data-sources/{e2e_config.source_id_pg}/catalog/scans")
    assert listed.status_code == 200
    assert any(scan["id"] == queued.json()["id"] for scan in listed.json())


async def test_the_scan_api_refuses_a_connector_without_a_collector(
    admin_session: Any, e2e_config: Any
) -> None:
    response = admin_session.client.post(
        f"/api/data-sources/{e2e_config.source_id_http}/catalog/scans",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert response.status_code == 409, response.text


# --------------------------------------------------------------------------
# Queue guarantees, on the real indexes
# --------------------------------------------------------------------------


async def test_a_source_never_has_two_waiting_scans(catalog_db: Any, e2e_config: Any) -> None:
    source_id = f"e2e_catalog_queue_{secrets.token_hex(4)}"
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    try:
        # Far in the future, so no worker claims it between the two requests.
        first = await enqueue_catalog_scan(
            catalog_db, source_id, trigger="manual", delay_seconds=3600
        )
        second = await enqueue_catalog_scan(
            catalog_db, source_id, trigger="save", delay_seconds=3600
        )
        assert first is not None and first == second
        assert (
            await catalog_db.fetchval(
                "SELECT count(*) FROM source_catalog_scans WHERE source_id = $1 AND status = 'pending'",
                source_id,
            )
            == 1
        )
    finally:
        await _drop_source(catalog_db, source_id)


async def test_a_request_brings_a_waiting_scheduled_scan_forward(
    catalog_db: Any, e2e_config: Any
) -> None:
    source_id = f"e2e_catalog_forward_{secrets.token_hex(4)}"
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    try:
        # An hour out, as a scheduled scan's jitter or a save's debounce can be.
        scheduled = await enqueue_catalog_scan(
            catalog_db, source_id, trigger="scheduled", delay_seconds=3600
        )
        # A worker may claim the scan the moment it is due; these columns are
        # set by the request itself, so they hold either way.
        asked = await enqueue_catalog_scan(
            catalog_db, source_id, trigger="manual", requested_by="someone", delay_seconds=0
        )
        assert asked == scheduled
        row = await catalog_db.fetchrow(
            "SELECT trigger, requested_by, not_before <= NOW() AS due "
            "FROM source_catalog_scans WHERE id = $1",
            scheduled,
        )
        assert dict(row) == {"trigger": "manual", "requested_by": "someone", "due": True}
    finally:
        await _drop_source(catalog_db, source_id)


async def test_a_source_is_never_scanned_twice_at_once(catalog_db: Any, e2e_config: Any) -> None:
    source_id = f"e2e_catalog_running_{secrets.token_hex(4)}"
    await _insert_source(catalog_db, e2e_config, source_id, enabled=False)
    try:
        insert = (
            "INSERT INTO source_catalog_scans (source_id, trigger, status, lease_expires_at) "
            "VALUES ($1, 'manual', 'running', NOW() + interval '1 hour')"
        )
        await catalog_db.execute(insert, source_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await catalog_db.execute(insert, source_id)
    finally:
        await _drop_source(catalog_db, source_id)


# --------------------------------------------------------------------------
# Store rules, with snapshots shaped exactly
# --------------------------------------------------------------------------


def _column(table: str, name: str, data_type: str = "text") -> CatalogNode:
    return CatalogNode(
        node_type="column",
        path=("s", table, name),
        name=name,
        resource_key=f"s.{table}.{name}",
        data_type=data_type,
    )


def _table(name: str) -> CatalogNode:
    return CatalogNode(node_type="table", path=("s", name), name=name, resource_key=f"s.{name}")


@pytest.fixture
async def store_source(catalog_db: Any, e2e_config: Any) -> Any:
    """A disabled source, so no worker ever scans it while snapshots are applied
    by hand."""
    source_id = f"e2e_catalog_store_{secrets.token_hex(4)}"
    await _insert_source(catalog_db, e2e_config, source_id, enabled=False)
    try:
        yield source_id
    finally:
        await _drop_source(catalog_db, source_id)


async def _apply(
    catalog_db: Any, source_id: str, snapshot: CatalogSnapshot, *, max_change_rows: int = 5000
) -> tuple[int, Any]:
    scan_id = await catalog_db.fetchval(
        "INSERT INTO source_catalog_scans (source_id, trigger, status) "
        "VALUES ($1, 'manual', 'running') RETURNING id",
        source_id,
    )
    result = await apply_snapshot(
        catalog_db,
        source_id=source_id,
        scan_id=scan_id,
        snapshot=snapshot,
        max_change_rows=max_change_rows,
    )
    await catalog_db.execute(
        "UPDATE source_catalog_scans SET status = 'succeeded', lease_expires_at = NULL WHERE id = $1",
        scan_id,
    )
    return scan_id, result


async def test_a_truncated_scan_never_marks_what_it_did_not_reach_as_removed(
    catalog_db: Any, store_source: str
) -> None:
    full = CatalogSnapshot(
        nodes=[_table("a"), _column("a", "x"), _table("b"), _column("b", "y")],
        complete_subtrees=[()],
    )
    await _apply(catalog_db, store_source, full)

    capped = CatalogSnapshot(
        nodes=[_table("a"), _column("a", "x")],
        truncated=True,
        truncation={"max_tables": 1},
        complete_subtrees=[],
    )
    _, result = await _apply(catalog_db, store_source, capped)
    assert result.removed == 0
    assert ("s", "b") in await _live_nodes(catalog_db, store_source)


async def test_a_complete_scan_marks_missing_nodes_removed(
    catalog_db: Any, store_source: str
) -> None:
    await _apply(
        catalog_db,
        store_source,
        CatalogSnapshot(nodes=[_table("a"), _table("b")], complete_subtrees=[()]),
    )
    _, result = await _apply(
        catalog_db, store_source, CatalogSnapshot(nodes=[_table("a")], complete_subtrees=[()])
    )
    assert result.removed == 1
    assert ("s", "b") not in await _live_nodes(catalog_db, store_source)


async def test_a_changed_type_is_recorded_as_a_change(catalog_db: Any, store_source: str) -> None:
    await _apply(
        catalog_db,
        store_source,
        CatalogSnapshot(nodes=[_table("a"), _column("a", "x", "text")], complete_subtrees=[()]),
    )
    scan_id, result = await _apply(
        catalog_db,
        store_source,
        CatalogSnapshot(nodes=[_table("a"), _column("a", "x", "integer")], complete_subtrees=[()]),
    )
    assert result.changed == 1
    change = await catalog_db.fetchrow(
        "SELECT change, before, after FROM source_catalog_changes WHERE scan_id = $1", scan_id
    )
    assert change["change"] == "changed"
    assert change["before"]["data_type"] == "text"
    assert change["after"]["data_type"] == "integer"


async def test_change_rows_are_capped_and_the_cap_is_reported(
    catalog_db: Any, store_source: str
) -> None:
    await _apply(catalog_db, store_source, CatalogSnapshot(nodes=[], complete_subtrees=[()]))
    many = [_table(f"t{i}") for i in range(10)]
    scan_id, result = await _apply(
        catalog_db,
        store_source,
        CatalogSnapshot(nodes=many, complete_subtrees=[()]),
        max_change_rows=3,
    )
    assert result.added == 10
    assert result.changes_truncated is True
    assert (
        await catalog_db.fetchval(
            "SELECT count(*) FROM source_catalog_changes WHERE scan_id = $1", scan_id
        )
        == 3
    )


async def test_attributes_are_stored_as_objects_not_strings(
    catalog_db: Any, store_source: str
) -> None:
    """The JSONB double-encoding that migration 015 repaired must not come back
    through the catalog's staging tables."""
    await _apply(
        catalog_db,
        store_source,
        CatalogSnapshot(
            nodes=[
                CatalogNode(
                    node_type="table",
                    path=("s", "a"),
                    name="a",
                    resource_key="s.a",
                    attributes={"row_estimate": 3},
                )
            ],
            complete_subtrees=[()],
        ),
    )
    kind = await catalog_db.fetchval(
        "SELECT jsonb_typeof(attributes) FROM source_catalog WHERE source_id = $1 AND path = $2",
        store_source,
        ["s", "a"],
    )
    assert kind == "object"


# --------------------------------------------------------------------------
# Scheduled refresh and retention
# --------------------------------------------------------------------------


def _scheduler_config(**overrides: Any) -> Any:
    from interlock.config import CatalogConfig

    return CatalogConfig(scheduler_jitter_seconds=0, **overrides)


@pytest.fixture
async def control_pool(e2e_config: Any) -> Any:
    pool = await asyncpg.create_pool(e2e_config.control_dsn, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


async def test_the_scheduler_queues_a_never_scanned_source_once_across_workers(
    catalog_db: Any, control_pool: Any, e2e_config: Any
) -> None:
    from interlock.catalog.scheduler import CatalogScheduler

    source_id = f"e2e_catalog_sched_{secrets.token_hex(4)}"
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    try:
        first = CatalogScheduler(pg_pool=control_pool, config=_scheduler_config())
        second = CatalogScheduler(pg_pool=control_pool, config=_scheduler_config())
        # Two schedulers ticking at the same moment, as two workers would.
        await asyncio.gather(first.tick(), second.tick())
        scheduled = await catalog_db.fetch(
            "SELECT id FROM source_catalog_scans WHERE source_id = $1", source_id
        )
        assert len(scheduled) == 1, "two ticking schedulers must queue one scan"

        scan = await _wait_for_scan_id(catalog_db, scheduled[0]["id"])
        assert scan["status"] == "succeeded", dict(scan)
        assert scan["trigger"] == "scheduled"

        # Fresh now: another tick leaves it alone...
        await first.tick()
        assert (
            await catalog_db.fetchval(
                "SELECT count(*) FROM source_catalog_scans WHERE source_id = $1", source_id
            )
            == 1
        )
        # ...until its interval has passed.
        from datetime import timedelta

        later = scan["finished_at"] + timedelta(
            seconds=_scheduler_config().refresh_interval_seconds
        )
        await first.tick(now=later)
        assert (
            await catalog_db.fetchval(
                "SELECT count(*) FROM source_catalog_scans WHERE source_id = $1", source_id
            )
            == 2
        )
    finally:
        await _drop_source(catalog_db, source_id)


async def test_a_source_can_opt_out_of_scheduled_refresh(
    catalog_db: Any, control_pool: Any, e2e_config: Any
) -> None:
    from interlock.catalog.scheduler import CatalogScheduler

    source_id = f"e2e_catalog_optout_{secrets.token_hex(4)}"
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    await catalog_db.execute(
        "UPDATE data_sources SET metadata = metadata || $2::jsonb WHERE source_id = $1",
        source_id,
        {"catalog_refresh_interval_seconds": 0},
    )
    try:
        await CatalogScheduler(pg_pool=control_pool, config=_scheduler_config()).tick()
        assert (
            await catalog_db.fetchval(
                "SELECT count(*) FROM source_catalog_scans WHERE source_id = $1", source_id
            )
            == 0
        )
    finally:
        await _drop_source(catalog_db, source_id)


async def test_retention_keeps_only_the_newest_finished_scans(
    catalog_db: Any, control_pool: Any, store_source: str
) -> None:
    from interlock.catalog.scheduler import CatalogScheduler

    for _ in range(6):
        await catalog_db.execute(
            "INSERT INTO source_catalog_scans (source_id, trigger, status, finished_at) "
            "VALUES ($1, 'manual', 'succeeded', NOW())",
            store_source,
        )
    newest = await catalog_db.fetch(
        "SELECT id FROM source_catalog_scans WHERE source_id = $1 ORDER BY id DESC LIMIT 4",
        store_source,
    )
    await CatalogScheduler(
        pg_pool=control_pool, config=_scheduler_config(scan_history_retention=4)
    ).tick()
    kept = await catalog_db.fetch(
        "SELECT id FROM source_catalog_scans WHERE source_id = $1 ORDER BY id DESC",
        store_source,
    )
    assert [r["id"] for r in kept] == [r["id"] for r in newest]


# --------------------------------------------------------------------------
# Discovery follows the catalog
# --------------------------------------------------------------------------


async def _discovery_tables(control_db: Any, source_id: str) -> set[str]:
    rows = await control_db.fetch(
        "SELECT asset_path FROM discovery_assets WHERE source_id = $1 AND asset_type = 'table'",
        source_id,
    )
    return {row["asset_path"] for row in rows}


async def _wait_for_discovery(
    control_db: Any, source_id: str, predicate: Any, *, what: str
) -> set[str]:
    deadline = asyncio.get_running_loop().time() + _SCAN_TIMEOUT_SECONDS
    tables: set[str] = set()
    while asyncio.get_running_loop().time() < deadline:
        tables = await _discovery_tables(control_db, source_id)
        if predicate(tables):
            return tables
        await asyncio.sleep(1)
    raise AssertionError(f"discovery never showed {what}: {sorted(tables)}")


async def test_discovery_indexes_tables_from_the_catalog_and_prunes_removed_ones(
    catalog_db: Any, e2e_config: Any
) -> None:
    source_id = f"e2e_catalog_disc_{secrets.token_hex(4)}"
    table = f"catalog_disc_{secrets.token_hex(4)}"
    upstream = await asyncpg.connect(e2e_config.source_dsn)
    await upstream.execute(f"CREATE TABLE {table} (id int, email text)")
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    try:
        assert (await _scan_now(catalog_db, source_id))["status"] == "succeeded"
        await _wait_for_discovery(
            catalog_db,
            source_id,
            lambda tables: f"public.{table}" in tables and "public.customers" in tables,
            what=f"public.{table}",
        )
        summary = await catalog_db.fetchval(
            "SELECT summary FROM discovery_assets WHERE source_id = $1 AND asset_path = $2",
            source_id,
            f"public.{table}",
        )
        # Columns reach discovery in catalog order, as the asset's entities.
        assert summary["entities"] == ["id", "email"]

        await upstream.execute(f"DROP TABLE {table}")
        assert (await _scan_now(catalog_db, source_id))["status"] == "succeeded"
        await _wait_for_discovery(
            catalog_db,
            source_id,
            lambda tables: f"public.{table}" not in tables and "public.customers" in tables,
            what=f"public.{table} pruned",
        )
    finally:
        await upstream.execute(f"DROP TABLE IF EXISTS {table}")
        await upstream.close()
        await catalog_db.execute("DELETE FROM discovery_assets WHERE source_id = $1", source_id)
        await _drop_source(catalog_db, source_id)


async def test_the_discovery_rescan_queues_catalog_scans(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    response = admin_session.client.post(
        "/dashboard/discovery/rescan", headers={"X-CSRF-Token": admin_session.csrf_token}
    )
    assert response.status_code == 200, response.text[:300]
    assert "Queued a catalog scan for" in response.text
    assert e2e_config.source_id_pg in response.text
    # An HTTP source has no collector, and is reported as skipped, not failed.
    assert "skipped" in response.text and e2e_config.source_id_http in response.text
    scan = await _wait_for_scan(catalog_db, e2e_config.source_id_pg)
    assert scan["status"] == "succeeded", dict(scan)


# --------------------------------------------------------------------------
# The admin's catalog views
# --------------------------------------------------------------------------


async def test_source_detail_shows_the_catalog_and_rescans(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    source_id = e2e_config.source_id_pg
    await _scan_now(catalog_db, source_id)
    detail = admin_session.client.get(f"/dashboard/data-sources/{source_id}")
    assert f'hx-get="/dashboard/data-sources/{source_id}/catalog"' in detail.text

    section = admin_session.client.get(f"/dashboard/data-sources/{source_id}/catalog")
    assert section.status_code == 200, section.text[:300]
    assert "succeeded" in section.text and "<code>public</code>" in section.text

    children = admin_session.client.get(
        "/dashboard/catalog/children",
        params={"source_id": source_id, "path": json.dumps(["public", "customers"])},
    )
    assert children.status_code == 200
    assert "<code>email</code>" in children.text
    assert "likely PII" in children.text

    # The PII filter finds columns by name until an admin has classified them.
    found = admin_session.client.get(
        "/dashboard/catalog", params={"source": source_id, "classification": "pii_any"}
    )
    assert "public.customers.ssn" in found.text
    assert "public.customers.id<" not in found.text

    rescan = admin_session.client.post(
        f"/dashboard/data-sources/{source_id}/catalog/rescan",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert rescan.status_code == 200 and "queued" in rescan.text
    scan = await _wait_for_scan(catalog_db, source_id)
    assert scan["requested_by"] == e2e_config.admin_username


async def test_an_admin_annotation_survives_a_rescan(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    source_id = f"e2e_catalog_annot_{secrets.token_hex(4)}"
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    path = ["public", "customers", "email"]
    try:
        assert (await _scan_now(catalog_db, source_id))["status"] == "succeeded"
        response = admin_session.client.post(
            f"/dashboard/data-sources/{source_id}/catalog/annotations",
            data={"path": json.dumps(path), "classification": "sensitive"},
            headers={"X-CSRF-Token": admin_session.csrf_token},
        )
        assert response.status_code == 200, response.text[:300]
        assert '<option value="sensitive" selected>' in response.text

        assert (await _scan_now(catalog_db, source_id))["status"] == "succeeded"
        row = await catalog_db.fetchrow(
            "SELECT classification, classification_source, applied_by "
            "FROM source_catalog_annotations WHERE source_id = $1 AND path = $2",
            source_id,
            path,
        )
        assert dict(row) == {
            "classification": "sensitive",
            "classification_source": "admin",
            "applied_by": e2e_config.admin_username,
        }
        found = admin_session.client.get(
            "/dashboard/catalog", params={"q": "email", "classification": "sensitive"}
        )
        assert source_id in found.text

        # A path the catalog has never seen is refused rather than stored.
        unknown = admin_session.client.post(
            f"/dashboard/data-sources/{source_id}/catalog/annotations",
            data={"path": json.dumps(["public", "nope", "x"]), "classification": "pii"},
            headers={"X-CSRF-Token": admin_session.csrf_token},
        )
        assert unknown.status_code == 404
    finally:
        await _drop_source(catalog_db, source_id)


async def test_drift_can_be_acknowledged(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    source_id = f"e2e_catalog_ack_{secrets.token_hex(4)}"
    table = f"catalog_ack_{secrets.token_hex(4)}"
    upstream = await asyncpg.connect(e2e_config.source_dsn)
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    try:
        await _scan_now(catalog_db, source_id)
        await upstream.execute(f"CREATE TABLE {table} (id int)")
        await _scan_now(catalog_db, source_id)
        section = admin_session.client.get(f"/dashboard/data-sources/{source_id}/catalog")
        assert f"public.{table}" in section.text

        acknowledged = admin_session.client.post(
            f"/dashboard/data-sources/{source_id}/catalog/changes/acknowledge",
            headers={"X-CSRF-Token": admin_session.csrf_token},
        )
        assert acknowledged.status_code == 200
        assert "No unacknowledged changes" in acknowledged.text
        open_changes = await catalog_db.fetchval(
            "SELECT count(*) FROM source_catalog_changes "
            "WHERE source_id = $1 AND acknowledged_at IS NULL",
            source_id,
        )
        assert open_changes == 0
    finally:
        await upstream.execute(f"DROP TABLE IF EXISTS {table}")
        await upstream.close()
        await _drop_source(catalog_db, source_id)


async def test_wizard_pii_flags_become_catalog_annotations(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    source_id = f"e2e_catalog_wizpii_{secrets.token_hex(4)}"
    form = {
        **_pg_form(e2e_config, source_id),
        "pii_columns": [
            json.dumps(["public", "customers", "email"]),
            json.dumps(["sales", "odd.table", "ssn"]),
        ],
    }
    try:
        response = admin_session.client.post(
            "/dashboard/source-wizard/save",
            data=form,
            headers={"X-CSRF-Token": admin_session.csrf_token},
        )
        assert response.status_code == 303, response.text[:300]
        rows = await catalog_db.fetch(
            "SELECT path, classification, classification_source FROM source_catalog_annotations "
            "WHERE source_id = $1 ORDER BY path",
            source_id,
        )
        assert [
            (list(r["path"]), r["classification"], r["classification_source"]) for r in rows
        ] == [
            (["public", "customers", "email"], "pii", "wizard"),
            (["sales", "odd.table", "ssn"], "pii", "wizard"),
        ]
    finally:
        await _drop_source(catalog_db, source_id)


# --------------------------------------------------------------------------
# Pickers and warnings in the role and policy editors (PR 5)
# --------------------------------------------------------------------------


async def test_the_role_editor_warns_against_the_catalog(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    await _scan_now(catalog_db, e2e_config.source_id_pg)
    response = admin_session.client.post(
        f"/dashboard/data-sources/{e2e_config.source_id_pg}/roles/lint",
        data={
            "permission_effect": ["allow", "allow", "allow"],
            "permission_action": ["db.table.select"] * 3,
            "permission_resource_type": ["db.table", "db.column", "db.table"],
            "permission_resource_pattern": [
                "public.customers.*",
                "public.customers.email",
                "public.no_such_table.*",
            ],
        },
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert response.status_code == 200, response.text[:300]
    assert "public.no_such_table.*" in response.text and "statement 3" in response.text
    assert "Column-level allow" in response.text
    assert "statement 1" not in response.text


async def test_the_role_editor_flags_legacy_mysql_spellings(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    await _scan_now(catalog_db, e2e_config.source_id_mysql)
    response = admin_session.client.post(
        f"/dashboard/data-sources/{e2e_config.source_id_mysql}/roles/lint",
        data={
            "permission_effect": "allow",
            "permission_action": "db.table.select",
            "permission_resource_type": "db.table",
            "permission_resource_pattern": "public.customers.*",
        },
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert "legacy alias" in response.text and "source_db.customers.*" in response.text


async def test_the_picker_offers_catalogued_tables_and_columns(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    await _scan_now(catalog_db, e2e_config.source_id_pg)
    form = admin_session.client.get(f"/dashboard/data-sources/{e2e_config.source_id_pg}/roles/new")
    assert "Pick from the catalog" in form.text and 'data-catalog-pick="pattern"' in form.text
    columns = admin_session.client.get(
        "/dashboard/catalog/children",
        params={
            "source_id": e2e_config.source_id_pg,
            "path": json.dumps(["public", "customers"]),
            "pick": "1",
        },
    )
    assert 'data-pattern="public.customers.ssn"' in columns.text
    picker = admin_session.client.get(
        "/dashboard/catalog/picker", params={"cond_source_id": e2e_config.source_id_pg}
    )
    assert "Pick from the catalog" in picker.text


async def test_the_picker_writes_object_storage_patterns_for_the_statement_type(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    """An S3 prefix picked into a role covers the objects under it."""
    await _scan_now(catalog_db, e2e_config.source_id_s3)
    top = admin_session.client.get(
        "/dashboard/catalog/children",
        params={"source_id": e2e_config.source_id_s3, "pick": "1"},
    )
    assert top.status_code == 200
    assert "js-catalog-pick" in top.text, top.text[:500]
    assert 'data-resource-type="storage.object"' in top.text
    assert (
        "&#34;storage.object&#34;: &#34;s3://" in top.text or '"storage.object": "s3://' in top.text
    )
    assert "/*" in top.text


async def test_policy_validation_reports_unknown_and_ambiguous_names(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    await _scan_now(catalog_db, e2e_config.source_id_pg)
    response = admin_session.client.post(
        "/api/policies/validate",
        json={
            "conditions": {
                "source_ids": [e2e_config.source_id_pg],
                "tables": ["customers", "invoices"],
                "columns": ["ssn", "no_such_column"],
            },
            "actions": {"effect": "deny"},
        },
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert response.status_code == 200, response.text
    codes = [(w["code"], w["message"]) for w in response.json()["warnings"]]
    assert any(code == "unknown_table" and "invoices" in msg for code, msg in codes)
    assert any(code == "unknown_column" and "no_such_column" in msg for code, msg in codes)
    assert not any("customers" in msg and code == "unknown_table" for code, msg in codes)


async def test_a_policy_saved_with_a_columns_condition_keeps_it(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    name = f"e2e-catalog-columns-{secrets.token_hex(4)}"
    try:
        response = admin_session.client.post(
            "/dashboard/policies/create",
            data={
                "name": name,
                "priority": "1",
                "cond_source_id": e2e_config.source_id_pg,
                "cond_columns": "customers.ssn, email",
                "action_effect": "deny",
                "enabled": "on",
            },
            headers={"X-CSRF-Token": admin_session.csrf_token},
        )
        assert response.status_code in (200, 303), response.text[:300]
        conditions = await catalog_db.fetchval(
            "SELECT conditions FROM policy_rules WHERE name = $1", name
        )
        assert conditions["columns"] == ["customers.ssn", "email"]
    finally:
        await catalog_db.execute("DELETE FROM policy_rules WHERE name = $1", name)
        await catalog_db.execute("SELECT pg_notify('onyx_policy_changed', 'e2e')")


# --------------------------------------------------------------------------
# Sources that are not SQL (PR 7)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "node_type"),
    [
        ("source_id_s3", "bucket"),
        ("source_id_spaces", "bucket"),
        ("source_id_opensearch", "index"),
        ("source_id_qdrant", "collection"),
        ("source_id_salesforce", "object"),
    ],
)
async def test_a_non_sql_source_is_catalogued(
    attribute: str, node_type: str, catalog_db: Any, e2e_config: Any
) -> None:
    source_id = (
        getattr(e2e_config, attribute, None)
        or {
            "source_id_s3": "e2e_s3",
            "source_id_spaces": "e2e_spaces",
            "source_id_opensearch": "e2e_opensearch",
            "source_id_qdrant": "e2e_qdrant",
            "source_id_salesforce": "e2e_salesforce",
        }[attribute]
    )
    scan = await _scan_now(catalog_db, source_id)
    assert scan["status"] == "succeeded", dict(scan)
    types = {row["node_type"] for row in (await _live_nodes(catalog_db, source_id)).values()}
    assert node_type in types, types


async def test_the_console_labels_a_non_sql_catalog_not_enforced(
    admin_session: Any, catalog_db: Any
) -> None:
    await _scan_now(catalog_db, "e2e_s3")
    section = admin_session.client.get("/dashboard/data-sources/e2e_s3/catalog")
    assert "Not enforced on agent paths yet" in section.text
    assert "bucket" in section.text


# --------------------------------------------------------------------------
# Drift exposure and access analytics (PR 8)
# --------------------------------------------------------------------------


async def test_a_new_table_records_which_roles_already_reach_it(
    catalog_db: Any, e2e_config: Any
) -> None:
    source_id = f"e2e_catalog_expose_{secrets.token_hex(4)}"
    table = f"catalog_expose_{secrets.token_hex(4)}"
    upstream = await asyncpg.connect(e2e_config.source_dsn)
    await _insert_source(catalog_db, e2e_config, source_id, enabled=True)
    role_ids = []
    for role_key, statements in (
        ("wide", [("allow", "public.*")]),
        ("guarded", [("allow", "*.*"), ("deny", f"public.{table}.*")]),
    ):
        role_id = await catalog_db.fetchval(
            "INSERT INTO source_roles (source_id, role_key, name) VALUES ($1, $2, $2) RETURNING id",
            source_id,
            role_key,
        )
        role_ids.append(role_id)
        for effect, pattern in statements:
            await catalog_db.execute(
                "INSERT INTO source_role_permissions "
                "(role_id, effect, action, resource_type, resource_pattern, constraints) "
                "VALUES ($1, $2, 'db.table.select', 'db.table', $3, '{}'::jsonb)",
                role_id,
                effect,
                pattern,
            )
    try:
        await _scan_now(catalog_db, source_id)
        await upstream.execute(f"CREATE TABLE {table} (id int)")
        scan = await _scan_now(catalog_db, source_id)
        exposure = await catalog_db.fetchval(
            "SELECT exposure FROM source_catalog_changes "
            "WHERE scan_id = $1 AND node_type = 'table' AND path = $2",
            scan["id"],
            ["public", table],
        )
        assert exposure is not None
        assert exposure["roles"] == ["wide"], exposure
    finally:
        await upstream.execute(f"DROP TABLE IF EXISTS {table}")
        await upstream.close()
        await _drop_source(catalog_db, source_id)


async def test_access_analytics_reads_the_audit_trail_against_the_catalog(
    admin_session: Any, catalog_db: Any, e2e_config: Any
) -> None:
    from tests.e2e.support.clients import mcp_call

    await _scan_now(catalog_db, e2e_config.source_id_pg)
    response = mcp_call(
        e2e_config,
        "interlock_query",
        {"source_id": e2e_config.source_id_pg, "sql": "SELECT email FROM customers LIMIT 1"},
    )
    assert response.status_code == 200, response.text
    page = None
    for _ in range(40):
        page = admin_session.client.get(
            "/dashboard/catalog/analytics", params={"source": e2e_config.source_id_pg}
        )
        if "public.customers.email" in page.text:
            break
        await asyncio.sleep(0.25)
    assert page is not None and page.status_code == 200
    assert "Most accessed tables" in page.text
    assert "public.customers.email" in page.text  # a likely-PII column that was read
