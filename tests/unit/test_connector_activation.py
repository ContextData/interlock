"""Only active connectors are offered, and every registration path enforces it."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.config import InterLockConfig
from interlock.connections import activation
from interlock.connections.connectors import CONNECTOR_DEFINITIONS

pytestmark = pytest.mark.real_connector_activation


class FakeConn:
    def __init__(self, parent: FakePool) -> None:
        self._parent = parent

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return await self._parent.fetch(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self._parent.writes.append((query, args))
        return "OK"

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakePool:
    def __init__(self, active: set[str]) -> None:
        self.active = active
        self.writes: list[tuple[str, tuple[Any, ...]]] = []
        self.sources: list[dict[str, Any]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM connector_activation" in query:
            return [{"connector_key": key} for key in sorted(self.active)]
        if "FROM data_sources" in query:
            return self.sources
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.writes.append((query, args))
        return "OK"

    def acquire(self) -> FakeConn:
        return FakeConn(self)


def _inserts(pool: FakePool) -> list[str]:
    return [q for q, _ in pool.writes if "INSERT INTO data_sources" in q]


@pytest.fixture
def pool() -> FakePool:
    return FakePool({"postgresql", "generic_rest"})


@pytest.fixture
async def client(pool: FakePool) -> Any:
    redis = MagicMock()
    redis.aclose = AsyncMock()
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as cp,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch("interlock.admin.app.create_redis_client", new_callable=AsyncMock) as cr,
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        cp.return_value = pool
        cr.return_value = redis
        app = create_app(config=InterLockConfig())
    app.state.pg_pool = pool
    app.state.redis = redis
    app.state.auth_disabled = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --- the module -------------------------------------------------------------


def test_planned_connectors_can_never_be_activated() -> None:
    planned = [k for k, d in CONNECTOR_DEFINITIONS.items() if d.status == "planned"]
    assert planned, "the registry has no planned connector to check"
    assert not any(activation.activatable(k) for k in planned)
    assert activation.activatable("postgresql")
    assert not activation.activatable("no_such_connector")


@pytest.mark.anyio
async def test_a_planned_connector_row_is_never_reported_active() -> None:
    keys = await activation.active_connector_keys(FakePool({"postgresql", "mongodb"}))
    assert keys == frozenset({"postgresql"})


def test_connectors_are_grouped_by_category_in_a_fixed_order() -> None:
    groups = activation.group_by_category(
        [CONNECTOR_DEFINITIONS[k] for k in ("slack", "s3", "generic_rest", "mysql", "github")]
    )
    assert [label for label, _ in groups] == [
        "Databases and warehouses",
        "Object storage",
        "Collaboration",
        "Code repositories",
        "Web APIs",
    ]


@pytest.mark.anyio
async def test_require_active_refuses_inactive_and_unknown_connectors() -> None:
    pool = FakePool({"postgresql"})
    assert (await activation.require_active(pool, "postgresql")).key == "postgresql"
    with pytest.raises(activation.ConnectorNotActiveError, match="MySQL.* is not active"):
        await activation.require_active(pool, "mysql")
    with pytest.raises(activation.ConnectorNotActiveError, match="Unknown connector"):
        await activation.require_active(pool, "no_such_connector")


# --- the console and API ------------------------------------------------------


@pytest.mark.anyio
async def test_new_source_dropdowns_list_only_active_connectors(client: AsyncClient) -> None:
    for url in ("/dashboard/data-sources/new", "/dashboard/source-wizard"):
        body = (await client.get(url)).text
        assert 'option value="postgresql"' in body, url
        assert 'option value="generic_rest"' in body, url
        assert 'option value="mysql"' not in body, url
        assert 'option value="snowflake"' not in body, url
        assert "Tier 1" not in body and "Tier 2" not in body, url
        assert 'optgroup label="Databases and warehouses"' in body, url


@pytest.mark.anyio
async def test_console_create_refuses_an_inactive_connector(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "hr",
            "name": "HR",
            "source_type": "mysql",
            "connector_key": "mysql",
            "host": "db.example.com",
        },
    )
    assert "MySQL / MariaDB connector is not active" in resp.text
    assert _inserts(pool) == []


@pytest.mark.anyio
async def test_console_create_refuses_an_unknown_connector_key(
    client: AsyncClient, pool: FakePool
) -> None:
    # An unknown key used to fall back to generic_rest and be accepted.
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "x",
            "name": "X",
            "source_type": "http",
            "connector_key": "no_such_connector",
            "base_url": "https://example.com",
        },
    )
    assert "Unknown connector" in resp.text
    assert _inserts(pool) == []


@pytest.mark.anyio
async def test_console_and_wizard_test_connection_refuse_an_inactive_connector(
    client: AsyncClient,
) -> None:
    form = {"source_type": "mysql", "connector_key": "mysql", "host": "db.example.com"}
    console = await client.post("/dashboard/data-sources/test", data=form)
    assert "is not active" in console.text
    wizard = await client.post("/dashboard/source-wizard/test", data=form)
    assert "is not active" in wizard.text


@pytest.mark.anyio
async def test_wizard_refuses_an_inactive_connector_at_step_two_and_at_save(
    client: AsyncClient, pool: FakePool
) -> None:
    form = {"source_id": "hr", "name": "HR", "source_type": "mysql", "connector_key": "mysql"}
    step2 = await client.post("/dashboard/source-wizard/step2", data=form)
    assert "is not active" in step2.text
    save = await client.post("/dashboard/source-wizard/save", data=form)
    assert "is not active" in save.text
    assert _inserts(pool) == []


@pytest.mark.anyio
async def test_api_create_and_test_refuse_an_inactive_connector(
    client: AsyncClient, pool: FakePool
) -> None:
    body = {
        "source_id": "hr",
        "name": "HR",
        "source_type": "mysql",
        "connector_key": "mysql",
        "connection_config": {"host": "db.example.com", "port": 3306},
    }
    created = await client.post("/api/data-sources", json=body)
    assert created.status_code == 422
    assert "is not active" in created.json()["detail"]
    assert _inserts(pool) == []
    tested = await client.post("/api/data-sources/test", json=body)
    assert tested.status_code == 422


@pytest.mark.anyio
async def test_connectors_page_splits_active_from_available(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.sources = [{"source_type": "postgresql", "metadata": {"connector_key": "postgresql"}}]
    body = (await client.get("/dashboard/connectors")).text
    active, _, available = body.partition("Available to activate")
    assert 'data-connector-key="postgresql"' in active
    assert 'data-connector-key="mysql"' in available
    assert 'data-connector-key="mongodb"' not in body  # planned: never shown


@pytest.mark.anyio
async def test_activation_is_stored_and_audited(client: AsyncClient, pool: FakePool) -> None:
    resp = await client.post("/dashboard/connectors/mysql/activation", data={"active": "on"})
    assert resp.status_code == 200
    upserts = [a for q, a in pool.writes if "INSERT INTO connector_activation" in q]
    assert upserts and upserts[0][:2] == ("mysql", True)
    audits = [a for q, a in pool.writes if "INSERT INTO admin_audit_log" in q]
    assert audits and audits[0][2] == "connector.activate"


@pytest.mark.anyio
async def test_a_planned_connector_cannot_be_activated(client: AsyncClient, pool: FakePool) -> None:
    resp = await client.post("/dashboard/connectors/mongodb/activation", data={"active": "on"})
    assert "cannot be activated" in resp.text
    assert not [q for q, _ in pool.writes if "connector_activation" in q]


@pytest.mark.anyio
async def test_api_lists_connectors_with_their_activation(client: AsyncClient) -> None:
    resp = await client.get("/api/connectors")
    assert resp.status_code == 200
    by_key = {row["key"]: row for row in resp.json()}
    assert by_key["postgresql"]["active"] is True
    assert by_key["mysql"]["active"] is False
    assert "mongodb" not in by_key


@pytest.mark.anyio
async def test_connectors_page_has_no_add_data_source_button(client: AsyncClient) -> None:
    """Sources are added from Data Sources; the Connectors page only manages activation."""
    body = (await client.get("/dashboard/connectors")).text
    assert "Add Data Source" not in body
    assert 'href="/dashboard/data-sources/new"' not in body


def test_default_active_connectors_match_what_migration_018_seeds() -> None:
    import re
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[2] / "migrations" / "018_connector_activation.sql"
    ).read_text()
    first_insert = body.split("INSERT INTO connector_activation", 2)[1]
    seeded = set(re.findall(r"\('([a-z0-9_]+)', TRUE\)", first_insert))
    assert seeded == set(activation.DEFAULT_ACTIVE_CONNECTOR_KEYS)
