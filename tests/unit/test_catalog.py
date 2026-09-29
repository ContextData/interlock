"""The source catalog's pure parts: snapshots, caps, scrubbing, queueing, hooks.

Database behaviour - drift, baselines, removal, concurrent claims - is proven
against a real control database in tests/e2e/test_source_catalog.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.catalog import errors
from interlock.catalog.collectors import collector_keys, has_collector
from interlock.catalog.collectors.mysql import build_mysql_snapshot
from interlock.catalog.collectors.postgres import build_postgres_snapshot
from interlock.catalog.model import CatalogNode, CatalogSnapshot, ScanLimits, sql_resource_key
from interlock.catalog.queue import enqueue_after_save, enqueue_catalog_scan

# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


def test_a_node_type_outside_the_schema_is_refused() -> None:
    with pytest.raises(ValueError):
        CatalogNode(node_type="spreadsheet", path=("x",), name="x", resource_key="x")


def test_the_attribute_hash_changes_only_with_what_a_rescan_should_call_a_change() -> None:
    base = CatalogNode(
        node_type="column",
        path=("s", "t", "c"),
        name="c",
        resource_key="s.t.c",
        data_type="text",
        ordinal=1,
        attributes={"nullable": True},
    )
    same = CatalogNode(
        node_type="column",
        path=("s", "t", "c"),
        name="c",
        resource_key="s.t.c",
        data_type="text",
        ordinal=1,
        attributes={"nullable": True},
    )
    retyped = CatalogNode(
        node_type="column",
        path=("s", "t", "c"),
        name="c",
        resource_key="s.t.c",
        data_type="integer",
        ordinal=1,
        attributes={"nullable": True},
    )
    assert base.attributes_hash == same.attributes_hash
    assert base.attributes_hash != retyped.attributes_hash


def test_completeness_is_prefix_based() -> None:
    snapshot = CatalogSnapshot(complete_subtrees=[("sales",)])
    assert snapshot.is_complete_under(("sales", "customers", "email"))
    assert not snapshot.is_complete_under(("hr", "employees"))
    assert CatalogSnapshot(complete_subtrees=[()]).is_complete_under(("anything",))
    assert not CatalogSnapshot().is_complete_under(("anything",))


def test_resource_keys_are_lower_cased_dotted_paths() -> None:
    assert sql_resource_key("Sales", "Customers", "Email") == "sales.customers.email"


# --------------------------------------------------------------------------
# PostgreSQL snapshot building, including caps
# --------------------------------------------------------------------------


_FACTS = {
    "search_path": '"$user", public',
    "effective_schemas": ["public"],
    "login": "interlock",
    "database": "app",
    "server_version": "16.4",
}


def _relation(
    schema: str,
    relation: str,
    column: str | None,
    ordinal: int = 1,
    relkind: str = "r",
    data_type: str = "text",
) -> dict[str, Any]:
    return {
        "schema_name": schema,
        "relation_name": relation,
        "relkind": relkind,
        "row_estimate": 10,
        "can_select": True,
        "column_name": column,
        "data_type": data_type,
        "is_nullable": True,
        "ordinal": ordinal,
    }


def test_postgres_snapshot_records_schemas_relations_and_columns() -> None:
    snapshot = build_postgres_snapshot(
        _FACTS,
        [{"schema_name": "public"}, {"schema_name": "sales"}],
        [
            _relation("sales", "customers", "id", 1, data_type="integer"),
            _relation("sales", "customers", "email", 2),
            _relation("sales", "monthly", "total", 1, relkind="m", data_type="numeric"),
            _relation("public", "v_orders", "id", 1, relkind="v"),
        ],
        ScanLimits(),
    )
    by_path = {node.path: node for node in snapshot.nodes}
    assert by_path[("sales",)].node_type == "schema"
    assert by_path[("sales", "customers")].node_type == "table"
    assert by_path[("sales", "monthly")].node_type == "materialized_view"
    assert by_path[("public", "v_orders")].node_type == "view"
    email = by_path[("sales", "customers", "email")]
    assert email.resource_key == "sales.customers.email"
    assert email.heuristic_class is not None
    assert snapshot.source_attributes["default_schema"] == "public"
    assert snapshot.complete_subtrees == [()]
    assert not snapshot.truncated


def test_a_relation_with_no_visible_columns_is_still_recorded() -> None:
    snapshot = build_postgres_snapshot(
        _FACTS, [{"schema_name": "public"}], [_relation("public", "empty", None)], ScanLimits()
    )
    assert ("public", "empty") in {node.path for node in snapshot.nodes}


def test_hitting_the_table_cap_truncates_and_limits_what_counts_as_complete() -> None:
    """A capped scan must say so, and must not claim completeness for the
    schema it stopped inside - otherwise the store would mark the tables it
    never reached as removed."""
    rows = [
        _relation("public", "a", "id"),
        _relation("sales", "b", "id"),
        _relation("sales", "c", "id"),
    ]
    snapshot = build_postgres_snapshot(
        _FACTS,
        [{"schema_name": "public"}, {"schema_name": "sales"}],
        rows,
        ScanLimits(max_tables=2),
    )
    assert snapshot.truncated
    assert snapshot.truncation == {"max_tables": 2}
    assert ("sales", "c") not in {node.path for node in snapshot.nodes}
    assert snapshot.complete_subtrees == [("public",)]
    assert not snapshot.is_complete_under(("sales", "c"))


def test_hitting_the_column_cap_truncates_that_schema() -> None:
    rows = [_relation("public", "wide", f"c{i}", i) for i in range(1, 6)]
    snapshot = build_postgres_snapshot(
        _FACTS, [{"schema_name": "public"}], rows, ScanLimits(max_columns_per_table=3)
    )
    columns = [n for n in snapshot.nodes if n.node_type == "column"]
    assert len(columns) == 3
    assert snapshot.truncation == {"max_columns_per_table": 3}
    assert snapshot.complete_subtrees == []


def test_hitting_the_schema_cap_claims_nothing_complete() -> None:
    snapshot = build_postgres_snapshot(
        _FACTS,
        [{"schema_name": "a"}, {"schema_name": "b"}],
        [],
        ScanLimits(max_schemas=1),
    )
    assert snapshot.truncation == {"max_schemas": 1}
    assert snapshot.complete_subtrees == []


# --------------------------------------------------------------------------
# MySQL snapshot building
# --------------------------------------------------------------------------


def test_mysql_snapshot_covers_every_schema_and_records_the_default() -> None:
    snapshot = build_mysql_snapshot(
        facts={"login": "u@%", "server_version": "11.4", "default_schema": "hr"},
        schema_rows=["hr", "sales"],
        relation_rows=[
            ("hr", "employees", "BASE TABLE", 5, "salary", "int(11)", "NO", 1),
            ("sales", "v_top", "VIEW", None, "id", "int(11)", "YES", 1),
        ],
        limits=ScanLimits(),
    )
    by_path = {node.path: node for node in snapshot.nodes}
    assert by_path[("hr", "employees")].node_type == "table"
    assert by_path[("sales", "v_top")].node_type == "view"
    assert by_path[("hr", "employees", "salary")].attributes == {"nullable": False}
    assert snapshot.source_attributes["default_schema"] == "hr"
    assert snapshot.complete_subtrees == [()]


# --------------------------------------------------------------------------
# Collector registry
# --------------------------------------------------------------------------


def test_the_connectors_that_can_enumerate_have_collectors() -> None:
    assert {
        "postgresql",
        "mysql",
        "mariadb",
        "snowflake",
        "s3",
        "digitalocean_spaces",
        "slack",
        "github",
        "salesforce",
        "opensearch",
        "elasticsearch",
        "qdrant",
    } <= collector_keys()
    # Deferred: nothing to enumerate generically, or not built yet.
    for key in ("generic_rest", "gitlab", "zendesk", "notion", "google_workspace"):
        assert not has_collector(key), key


# --------------------------------------------------------------------------
# Error scrubbing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "could not connect to postgresql://admin:hunter2@db.internal:5432/app",
        "auth failed: password=hunter2 for user admin",
        "token: xoxb-123456789-abcdef rejected",
    ],
)
def test_a_scan_error_never_carries_a_credential(message: str) -> None:
    code, scrubbed = errors.classify_error(RuntimeError(message))
    assert "hunter2" not in scrubbed
    assert "xoxb-123456789" not in scrubbed
    assert code


def test_error_codes_are_stable() -> None:
    assert errors.classify_error(asyncio.TimeoutError())[0] == "timeout"
    assert errors.classify_error(ConnectionRefusedError("refused"))[0] == "network"
    assert errors.classify_error(errors.CatalogScanError("unsupported", "x"))[0] == "unsupported"
    assert (
        errors.classify_error(RuntimeError("Verified upstream PostgreSQL TLS is required"))[0]
        == "tls_refused"
    )


def test_an_egress_refusal_is_reported_as_one() -> None:
    """Otherwise a source pointed at a private host fails as `internal`, which
    tells the admin nothing about the one setting that fixes it."""
    from interlock.errors import EgressBlockedError

    code, _ = errors.classify_error(EgressBlockedError("private address 10.0.0.5 is blocked"))
    assert code == "egress_blocked"


def test_a_long_error_is_bounded() -> None:
    _, scrubbed = errors.classify_error(RuntimeError("x" * 5000))
    assert len(scrubbed) <= 300


# --------------------------------------------------------------------------
# Queueing
# --------------------------------------------------------------------------


def _pool(source_row: dict[str, Any] | None, *, inserted: int | None, existing: int | None) -> Any:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=source_row)
    pool.fetchval = AsyncMock(side_effect=[inserted, existing])
    return pool


def _source(enabled: bool = True, source_type: str = "postgresql") -> dict[str, Any]:
    return {"source_type": source_type, "enabled": enabled, "metadata": {}}


async def test_a_scan_is_queued_for_an_enabled_sql_source() -> None:
    pool = _pool(_source(), inserted=7, existing=None)
    assert await enqueue_catalog_scan(pool, "sales_pg", trigger="save") == 7


async def test_a_second_request_returns_the_scan_already_waiting() -> None:
    """The partial unique index turns the second insert into a no-op; the
    caller still gets the waiting scan's id rather than None."""
    pool = _pool(_source(), inserted=None, existing=7)
    assert await enqueue_catalog_scan(pool, "sales_pg", trigger="manual") == 7


@pytest.mark.parametrize(
    "row",
    [None, _source(enabled=False), _source(source_type="saas")],
)
async def test_nothing_is_queued_for_an_unknown_disabled_or_uncollectable_source(
    row: dict[str, Any] | None,
) -> None:
    pool = _pool(row, inserted=1, existing=None)
    assert await enqueue_catalog_scan(pool, "x", trigger="save") is None
    pool.fetchval.assert_not_awaited()


async def test_metadata_stored_as_a_json_string_is_understood() -> None:
    pool = _pool(
        {"source_type": "saas", "enabled": True, "metadata": '{"connector_key": "mysql"}'},
        inserted=3,
        existing=None,
    )
    assert await enqueue_catalog_scan(pool, "hr", trigger="save") == 3


async def test_an_unknown_trigger_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        await enqueue_catalog_scan(MagicMock(), "x", trigger="cron")


async def test_queueing_after_a_save_never_fails_the_save() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=ConnectionError("control db down"))
    assert await enqueue_after_save(pool, "sales_pg") is None


# --------------------------------------------------------------------------
# The admin save hook
# --------------------------------------------------------------------------


def _request(catalog: Any) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(config=SimpleNamespace(catalog=catalog), pg_pool=MagicMock())
        ),
        state=SimpleNamespace(admin=SimpleNamespace(username="admin")),
    )


@pytest.mark.parametrize(
    "catalog",
    [
        SimpleNamespace(enabled=False, scan_on_save=True),
        SimpleNamespace(enabled=True, scan_on_save=False),
    ],
)
async def test_the_save_hook_respects_the_catalog_switches(
    catalog: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from interlock.admin import catalog_hooks

    enqueue = AsyncMock(return_value=1)
    monkeypatch.setattr(catalog_hooks, "enqueue_after_save", enqueue)
    assert await catalog_hooks.after_source_saved(_request(catalog), "sales_pg") is None
    enqueue.assert_not_awaited()


async def test_the_save_hook_records_who_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from interlock.admin import catalog_hooks

    enqueue = AsyncMock(return_value=4)
    monkeypatch.setattr(catalog_hooks, "enqueue_after_save", enqueue)
    request = _request(SimpleNamespace(enabled=True, scan_on_save=True))
    assert await catalog_hooks.after_source_saved(request, "sales_pg") == 4
    assert enqueue.await_args.kwargs["requested_by"] == "admin"


def test_every_source_save_path_queues_a_scan() -> None:
    """Six places store a source; a seventh added later without the hook would
    leave that source uncatalogued until the next scheduled refresh. The e2e
    suite proves each path end to end; this pins the call sites."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "interlock" / "admin" / "routes"
    api = (root / "data_sources.py").read_text()
    console = (root / "dashboard.py").read_text()
    assert api.count("await after_source_saved(request,") == 2
    assert console.count("await after_source_saved(request,") == 4


# --------------------------------------------------------------------------
# The scan runner's decisions
# --------------------------------------------------------------------------


class _FakePool:
    """Records every statement; `acquire` hands back itself as the connection."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"

    def acquire(self) -> Any:
        pool = self

        class _Ctx:
            async def __aenter__(self) -> Any:
                return pool

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _Ctx()

    def finish(self) -> tuple[Any, ...]:
        finishes = [args for sql, args in self.executed if "SET status = $2" in sql]
        assert len(finishes) == 1, self.executed
        return finishes[0]


def _runner(pool: Any, source: Any, *, timeout: int = 600, on_scanned: Any = None) -> Any:
    from interlock.catalog.runner import CatalogScanRunner
    from interlock.config import CatalogConfig

    registry = MagicMock()
    registry.load = AsyncMock()
    registry.get = MagicMock(return_value=source)
    return CatalogScanRunner(
        pg_pool=pool,
        registry=registry,
        conn_manager=MagicMock(),
        config=CatalogConfig(scan_timeout_seconds=max(timeout, 10)),
        worker_id="w1",
        on_scanned=on_scanned,
    )


def _data_source(enabled: bool = True, connector_key: str = "postgresql") -> Any:
    return SimpleNamespace(
        enabled=enabled, connector_key=connector_key, connection_config={"host": "db"}
    )


async def test_a_disabled_source_is_cancelled_without_connecting() -> None:
    pool = _FakePool()
    await _runner(pool, _data_source(enabled=False))._run(1, "sales_pg")
    status, error_code = pool.finish()[1], pool.finish()[11]
    assert (status, error_code) == ("cancelled", "disabled")


async def test_a_connector_without_a_collector_is_cancelled() -> None:
    pool = _FakePool()
    await _runner(pool, _data_source(connector_key="generic_rest"))._run(1, "public_api")
    assert (pool.finish()[1], pool.finish()[11]) == ("cancelled", "unsupported")


async def test_a_successful_scan_records_counts_truncation_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from interlock.catalog import runner as runner_module
    from interlock.catalog.store import ApplyResult

    snapshot = CatalogSnapshot(truncated=True, truncation={"max_tables": 5})

    async def collector(ctx: Any) -> CatalogSnapshot:
        assert ctx.connection_config == {"host": "db"}
        return snapshot

    async def apply(conn: Any, **kwargs: Any) -> ApplyResult:
        assert kwargs["snapshot"] is snapshot
        return ApplyResult(
            nodes_seen=9, added=2, removed=1, changed=3, baseline=False, changes_truncated=False
        )

    monkeypatch.setattr(runner_module, "collector_for", lambda key: collector)
    monkeypatch.setattr(runner_module, "apply_snapshot", apply)
    pool = _FakePool()
    await _runner(pool, _data_source())._run(1, "sales_pg")

    finish = pool.finish()
    assert finish[1] == "succeeded"
    assert finish[3:7] == (9, 2, 1, 3)
    assert finish[7] is True
    assert finish[8] == '{"max_tables": 5}'
    assert any("pg_notify" in sql for sql, _ in pool.executed)


async def test_a_failed_scan_records_a_scrubbed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from interlock.catalog import runner as runner_module

    async def collector(ctx: Any) -> CatalogSnapshot:
        raise RuntimeError("connect to postgresql://admin:hunter2@db:5432/app failed")

    monkeypatch.setattr(runner_module, "collector_for", lambda key: collector)
    pool = _FakePool()
    await _runner(pool, _data_source())._run(1, "sales_pg")

    finish = pool.finish()
    assert finish[1] == "failed"
    assert "hunter2" not in (finish[12] or "")
    assert not any("pg_notify" in sql for sql, _ in pool.executed)


async def test_a_scan_that_overruns_its_time_limit_fails_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from interlock.catalog import runner as runner_module

    async def collector(ctx: Any) -> CatalogSnapshot:
        await asyncio.sleep(3600)
        return CatalogSnapshot()

    monkeypatch.setattr(runner_module, "collector_for", lambda key: collector)
    runner = _runner(_FakePool(), _data_source())
    runner._config = runner._config.model_copy(update={"scan_timeout_seconds": 10})

    async def fast_wait_for(coro: Any, timeout: float) -> Any:
        coro.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(runner_module.asyncio, "wait_for", fast_wait_for)
    await runner._run(1, "sales_pg")
    assert runner._pool.finish()[1:2] == ("failed",)
    assert runner._pool.finish()[11] == "timeout"


def _apply_returning(**counts: Any) -> Any:
    from interlock.catalog.store import ApplyResult

    values = {"nodes_seen": 5, "added": 0, "removed": 0, "changed": 0, "baseline": False}
    values.update(counts)

    async def apply(conn: Any, **kwargs: Any) -> ApplyResult:
        return ApplyResult(changes_truncated=False, **values)

    return apply


@pytest.mark.parametrize(
    ("counts", "expect_hook"),
    [
        ({"baseline": True}, True),
        ({"added": 1}, True),
        ({"removed": 1}, True),
        ({"changed": 1}, True),
        ({}, False),
    ],
)
async def test_discovery_is_refreshed_only_when_a_scan_changed_something(
    monkeypatch: pytest.MonkeyPatch, counts: dict[str, Any], expect_hook: bool
) -> None:
    from interlock.catalog import runner as runner_module

    async def collector(ctx: Any) -> CatalogSnapshot:
        return CatalogSnapshot()

    monkeypatch.setattr(runner_module, "collector_for", lambda key: collector)
    monkeypatch.setattr(runner_module, "apply_snapshot", _apply_returning(**counts))
    hook = AsyncMock()
    await _runner(_FakePool(), _data_source(), on_scanned=hook)._run(1, "sales_pg")
    assert hook.await_count == (1 if expect_hook else 0)
    if expect_hook:
        hook.assert_awaited_with("sales_pg")


async def test_a_failing_post_scan_hook_does_not_fail_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from interlock.catalog import runner as runner_module

    async def collector(ctx: Any) -> CatalogSnapshot:
        return CatalogSnapshot()

    monkeypatch.setattr(runner_module, "collector_for", lambda key: collector)
    monkeypatch.setattr(runner_module, "apply_snapshot", _apply_returning(added=2))
    pool = _FakePool()
    hook = AsyncMock(side_effect=RuntimeError("discovery index unavailable"))
    await _runner(pool, _data_source(), on_scanned=hook)._run(1, "sales_pg")
    assert pool.finish()[1] == "succeeded"


# --------------------------------------------------------------------------
# The scheduler's choice of what is due
# --------------------------------------------------------------------------


def _state(**overrides: Any) -> Any:
    from interlock.catalog.scheduler import SourceScanState

    values: dict[str, Any] = {"source_id": "s", "connector_key": "postgresql"}
    values.update(overrides)
    return SourceScanState(**values)


def _due(*states: Any, interval: int = 86400, retry: int = 3600) -> list[str]:
    from datetime import UTC, datetime

    from interlock.catalog.scheduler import due_sources

    return due_sources(
        list(states),
        now=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
        default_interval=interval,
        failure_retry=retry,
    )


def _ago(seconds: int) -> Any:
    from datetime import UTC, datetime, timedelta

    return datetime(2026, 9, 25, 12, 0, tzinfo=UTC) - timedelta(seconds=seconds)


def test_a_never_scanned_source_is_due() -> None:
    assert _due(_state()) == ["s"]


def test_a_fresh_source_is_not_due_and_a_stale_one_is() -> None:
    assert _due(_state(last_success=_ago(3600))) == []
    assert _due(_state(last_success=_ago(86400))) == ["s"]


def test_a_source_with_a_scan_waiting_or_running_is_never_due() -> None:
    assert _due(_state(busy=True)) == []
    assert _due(_state(busy=True, last_success=_ago(10**6))) == []


def test_a_failed_source_retries_on_the_shorter_clock() -> None:
    fresh_failure = _state(last_success=_ago(10**6), last_failure=_ago(600))
    old_failure = _state(last_success=_ago(10**6), last_failure=_ago(3600))
    assert _due(fresh_failure) == []
    assert _due(old_failure) == ["s"]
    # A failure older than the last success does not hold a stale source back.
    recovered = _state(last_success=_ago(90000), last_failure=_ago(10**6))
    assert _due(recovered) == ["s"]


def test_a_source_can_set_its_own_interval_or_opt_out() -> None:
    key = "catalog_refresh_interval_seconds"
    assert _due(_state(metadata={key: 900}, last_success=_ago(1000))) == ["s"]
    assert _due(_state(metadata={key: 0}, last_success=_ago(10**7))) == []
    assert _due(_state(metadata={key: 0})) == []
    # A value that is not a number falls back to the default.
    assert _due(_state(metadata={key: "soon"}, last_success=_ago(3600))) == []


def test_a_source_without_a_collector_is_never_due() -> None:
    assert _due(_state(connector_key="generic_rest")) == []


async def test_a_tick_that_loses_the_lock_does_nothing() -> None:
    from interlock.catalog.scheduler import CatalogScheduler
    from interlock.config import CatalogConfig

    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=False)
    conn.fetch = AsyncMock()
    conn.execute = AsyncMock()

    class _Tx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    conn.transaction = MagicMock(return_value=_Tx())

    class _Acquire:
        async def __aenter__(self) -> Any:
            return conn

        async def __aexit__(self, *exc: Any) -> None:
            return None

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    assert await CatalogScheduler(pg_pool=pool, config=CatalogConfig()).tick() == []
    conn.fetch.assert_not_awaited()
    conn.execute.assert_not_awaited()
