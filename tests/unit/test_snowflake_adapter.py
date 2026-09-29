from __future__ import annotations

import pytest

from interlock.connections.connectors import get_adapter


class FakeCursor:
    def __init__(
        self,
        *,
        rows=None,
        one=None,
        description=None,
        sfqid: str | None = None,
        rowcount: int = -1,
    ) -> None:
        self.rows = rows or []
        self.one = one
        self.description = description or []
        self.sfqid = sfqid
        self.rowcount = rowcount
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one


class FakeSnowflakeConnection:
    def __init__(self, cursors: list[FakeCursor]) -> None:
        self.cursors = cursors
        self.closed = False
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        if not self.cursors:
            raise AssertionError("No fake Snowflake cursor available")
        return self.cursors.pop(0)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


SNOWFLAKE_CONFIG = {
    "account": "acme-test",
    "user": "svc_interlock",
    "password": "secret",
    "warehouse": "COMPUTE_WH",
    "database": "ANALYTICS",
    "schema": "PUBLIC",
    "role": "INTERLOCK_ROLE",
}


@pytest.mark.asyncio
async def test_snowflake_probe_uses_shared_connect_boundary(monkeypatch) -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    conn = FakeSnowflakeConnection([FakeCursor(one=(1,))])
    monkeypatch.setattr(adapter, "_connect_sync", lambda _config: conn)

    result = await adapter.probe(SNOWFLAKE_CONFIG)

    assert result.healthy is True
    assert conn.closed is True


@pytest.mark.asyncio
async def test_snowflake_introspect_and_list_assets(monkeypatch) -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    rows = [
        ("ANALYTICS", "PUBLIC", "CUSTOMERS", "ID", "NUMBER"),
        ("ANALYTICS", "PUBLIC", "CUSTOMERS", "EMAIL", "VARCHAR"),
        ("ANALYTICS", "PUBLIC", "ORDERS", "ID", "NUMBER"),
    ]
    conn = FakeSnowflakeConnection([FakeCursor(rows=rows)])
    monkeypatch.setattr(adapter, "_connect_sync", lambda _config: conn)

    result = await adapter.introspect(SNOWFLAKE_CONFIG)

    assert result["dialect"] == "snowflake"
    assert len(result["tables"]) == 2
    assert result["tables"][0]["columns"][0] == {"name": "ID", "type": "NUMBER"}

    conn = FakeSnowflakeConnection([FakeCursor(rows=rows)])
    monkeypatch.setattr(adapter, "_connect_sync", lambda _config: conn)
    assets = await adapter.list_assets(SNOWFLAKE_CONFIG)

    assert assets[0]["asset_path"] == "snowflake://ANALYTICS/PUBLIC/CUSTOMERS"
    assert assets[0]["asset_type"] == "table"
    assert assets[0]["metadata"]["warehouse"] == "COMPUTE_WH"


@pytest.mark.asyncio
async def test_snowflake_execute_read_returns_rows_and_cost_metadata(monkeypatch) -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    query_cursor = FakeCursor(
        rows=[(1, "ada@example.com")],
        description=[("ID",), ("EMAIL",)],
        sfqid="01abc",
    )
    history_cursor = FakeCursor(
        one=("01abc", 4096, 0, 1, 0.01),
        description=[
            ("QUERY_ID",),
            ("BYTES_SCANNED",),
            ("BYTES_WRITTEN",),
            ("ROWS_PRODUCED",),
            ("CREDITS_USED_CLOUD_SERVICES",),
        ],
    )
    conn = FakeSnowflakeConnection([query_cursor, history_cursor])
    monkeypatch.setattr(adapter, "_connect_sync", lambda _config: conn)

    result = await adapter.execute_read(
        {
            "source_id": "sf",
            "identity_id": 10,
            "query": "SELECT ID, EMAIL FROM ANALYTICS.PUBLIC.CUSTOMERS",
            "connection_config": SNOWFLAKE_CONFIG,
        }
    )

    assert result["rows"] == [{"ID": 1, "EMAIL": "ada@example.com"}]
    assert result["permission_request"]["action"] == "db.table.select"
    assert result["permission_request"]["resources"] == ["ANALYTICS.PUBLIC.CUSTOMERS"]
    assert result["cost_metadata"]["query_id"] == "01abc"
    assert result["cost_metadata"]["bytes_scanned"] == 4096
    assert result["cost_metadata"]["warehouse"] == "COMPUTE_WH"
    assert conn.closed is True


@pytest.mark.asyncio
async def test_snowflake_execute_read_rejects_multistatement_write_before_connect(
    monkeypatch,
) -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    monkeypatch.setattr(
        adapter,
        "_connect_sync",
        lambda _config: (_ for _ in ()).throw(AssertionError("should not connect")),
    )

    with pytest.raises(PermissionError, match="single read SQL"):
        await adapter.execute_read(
            {
                "source_id": "sf",
                "identity_id": 10,
                "query": "SELECT 1; DELETE FROM ANALYTICS.PUBLIC.CUSTOMERS WHERE ID = 1",
                "connection_config": SNOWFLAKE_CONFIG,
            }
        )


@pytest.mark.asyncio
async def test_snowflake_execute_write_commits_and_records_query_id(monkeypatch) -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    query_cursor = FakeCursor(sfqid="01write", rowcount=3)
    history_cursor = FakeCursor(one=None, description=[])
    conn = FakeSnowflakeConnection([query_cursor, history_cursor])
    monkeypatch.setattr(adapter, "_connect_sync", lambda _config: conn)

    result = await adapter.execute_write(
        {
            "source_id": "sf",
            "identity_id": 10,
            "query": "UPDATE ANALYTICS.PUBLIC.CUSTOMERS SET EMAIL = 'x@example.com'",
            "connection_config": SNOWFLAKE_CONFIG,
        }
    )

    assert result["affected_rows"] == 3
    assert result["permission_request"]["action"] == "db.table.update"
    assert result["cost_metadata"]["query_id"] == "01write"
    assert result["cost_metadata"]["affected_rows"] == 3
    assert conn.committed is True
    assert conn.closed is True


@pytest.mark.asyncio
async def test_snowflake_execute_write_rejects_multistatement_before_connect(
    monkeypatch,
) -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    monkeypatch.setattr(
        adapter,
        "_connect_sync",
        lambda _config: (_ for _ in ()).throw(AssertionError("should not connect")),
    )

    with pytest.raises(PermissionError, match="single write SQL"):
        await adapter.execute_write(
            {
                "source_id": "sf",
                "identity_id": 10,
                "query": "SELECT 1; DELETE FROM ANALYTICS.PUBLIC.CUSTOMERS WHERE ID = 1",
                "connection_config": SNOWFLAKE_CONFIG,
            }
        )


def test_snowflake_sanitize_masks_password_and_private_key() -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    safe = adapter.sanitize_config_for_admin(
        {
            **SNOWFLAKE_CONFIG,
            "private_key": "raw-key",
            "private_key_ref": "env://SNOWFLAKE_PRIVATE_KEY",
        }
    )

    assert safe["password"] == "<configured>"
    assert safe["private_key"] == "<configured>"
    assert safe["private_key_ref"] == "env:SNOWFLAKE_PRIVATE_KEY"
