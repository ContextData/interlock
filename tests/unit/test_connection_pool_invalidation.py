"""A pooled connection must not outlive the configuration that created it.

`get_pool` used to return a cached pool *before* consulting the registry, and
nothing ever evicted one. Repointing a source at a different host, correcting
a misconfiguration, or rotating a credential therefore had no effect until the
gateway process restarted: a revoked credential kept working through the proxy
indefinitely, while the admin console reported the change saved.

That is the same shape as the Write Safety defect that prompted the governance
audit - the system reports success while behaviour is unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from interlock.connections.manager import ConnectionManager
from interlock.errors import DataSourceUnavailableError


class _Config:
    """Minimal stand-in for a registered source."""

    def __init__(self, host: str = "db.example.com", **overrides: Any) -> None:
        self.host = host
        self.port = 5432
        self.database = "app"
        self.connection_string = None
        self.connection_config: dict[str, Any] = {"user": "svc", "password": "p"}
        self.enabled = True
        for key, value in overrides.items():
            setattr(self, key, value)


class _Registry:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.reads = 0

    def get(self, source_id: str) -> Any:
        self.reads += 1
        return self.config


class _Pool:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def manager(monkeypatch) -> tuple[ConnectionManager, _Registry, list[_Pool]]:
    registry = _Registry(_Config())
    created: list[_Pool] = []

    async def fake_create_pool(**kwargs: Any) -> _Pool:
        pool = _Pool(tag=str(kwargs.get("host")))
        created.append(pool)
        return pool

    monkeypatch.setattr("interlock.connections.manager.asyncpg.create_pool", fake_create_pool)
    monkeypatch.setattr(
        "interlock.connections.manager.validate_source_config", lambda *a, **k: None
    )

    class _Connector:
        key = "postgresql"
        credential_fields: tuple[str, ...] = ()
        secret_fields: tuple[str, ...] = ()

    monkeypatch.setattr("interlock.connections.manager.get_connector", lambda *a, **k: _Connector())

    class _SourceType:
        value = "postgresql"

    registry.config.source_type = _SourceType()
    registry.config.metadata = {}
    return ConnectionManager(registry), registry, created  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_unchanged_config_reuses_the_pool(manager) -> None:
    """The pool must still be a pool - the fix must not open one per request."""
    mgr, _registry, created = manager

    first = await mgr.get_pool("src")
    second = await mgr.get_pool("src")

    assert first is second
    assert len(created) == 1


@pytest.mark.asyncio
async def test_the_registry_is_consulted_before_a_pool_is_reused(manager) -> None:
    """The lookup used to short-circuit before this, which was the whole defect."""
    mgr, registry, _created = manager

    await mgr.get_pool("src")
    reads_after_first = registry.reads
    await mgr.get_pool("src")

    assert registry.reads > reads_after_first, (
        "get_pool returned a cached pool without re-reading the registry, so a "
        "configuration change would be invisible"
    )


@pytest.mark.asyncio
async def test_repointing_a_source_rebuilds_the_pool(manager) -> None:
    """The reproduction from the finding, in miniature."""
    mgr, registry, created = manager

    original = await mgr.get_pool("src")
    registry.config.host = "elsewhere.example.com"
    rebuilt = await mgr.get_pool("src")

    assert rebuilt is not original, "the pool survived a change of host"
    assert rebuilt.tag == "elsewhere.example.com"
    assert original.closed, "the stale pool was discarded without being closed"


@pytest.mark.asyncio
async def test_rotating_an_inline_credential_rebuilds_the_pool(manager) -> None:
    """The case with security consequences: a revoked credential must stop working."""
    mgr, registry, _created = manager

    original = await mgr.get_pool("src")
    registry.config.connection_config = {"user": "svc", "password": "rotated"}
    rebuilt = await mgr.get_pool("src")

    assert rebuilt is not original
    assert original.closed


@pytest.mark.asyncio
async def test_disabling_a_source_takes_effect_without_a_restart(manager) -> None:
    mgr, registry, _created = manager

    await mgr.get_pool("src")
    registry.config.enabled = False

    with pytest.raises(DataSourceUnavailableError):
        await mgr.get_pool("src")


@pytest.mark.asyncio
async def test_pools_for_different_roles_stay_separate(manager) -> None:
    """Role-mapped pools must not collapse into one another."""
    mgr, _registry, _created = manager

    reader = await mgr.get_pool("src", "readonly")
    writer = await mgr.get_pool("src", "readwrite")

    assert reader is not writer


@pytest.mark.asyncio
async def test_invalidate_closes_and_forgets_pools(manager) -> None:
    """The escape hatch for a rotation the stored config cannot show.

    A secret reached by reference - `env://NAME` now holding a new password -
    leaves the configuration byte-identical, so the fingerprint cannot see it.
    `invalidate` is what an operator calls in that case, and it existing is
    the difference between a documented limitation and a dead end.
    """
    mgr, _registry, _created = manager

    original = await mgr.get_pool("src")
    removed = await mgr.invalidate("src")
    rebuilt = await mgr.get_pool("src")

    assert removed == 1
    assert original.closed
    assert rebuilt is not original


@pytest.mark.asyncio
async def test_invalidate_without_a_source_clears_everything(manager) -> None:
    mgr, _registry, _created = manager

    await mgr.get_pool("src", "readonly")
    await mgr.get_pool("src", "readwrite")

    assert await mgr.invalidate() == 2
    assert await mgr.invalidate() == 0


@pytest.mark.asyncio
async def test_a_pool_that_refuses_to_close_still_gets_replaced(manager) -> None:
    """Cleanup failure must not strand the caller on a stale connection."""
    mgr, registry, _created = manager

    original = await mgr.get_pool("src")

    async def refuse() -> None:
        raise RuntimeError("close failed")

    original.close = refuse  # type: ignore[method-assign]
    registry.config.host = "elsewhere.example.com"

    rebuilt = await mgr.get_pool("src")

    assert rebuilt is not original
