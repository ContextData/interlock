"""Unit tests for the Gateway application shell."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette

from interlock.config import InterLockConfig
from interlock.db.migrations import discover_migrations
from interlock.errors import ConfigValidationError
from interlock.gateway.app import create_app

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> InterLockConfig:
    """Return an InterLockConfig with defaults (no YAML file needed)."""
    return InterLockConfig()


def _patch_infrastructure():
    """Return a context-manager stack that mocks all external I/O.

    Patches: create_pg_pool, close_pg_pool, create_redis_client,
    close_redis_client, MetadataRegistry, PGProxy (start/stop).
    """
    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    # MetadataRegistry.load does a PG fetch - mock the pool.fetch
    mock_pool.fetch = AsyncMock(return_value=[])
    mock_pool.fetchval = AsyncMock(return_value=1)

    mock_redis = MagicMock()
    mock_redis.aclose = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    patches = {
        "create_pg_pool": patch(
            "interlock.gateway.app.create_pg_pool",
            new=AsyncMock(return_value=mock_pool),
        ),
        "close_pg_pool": patch(
            "interlock.gateway.app.close_pg_pool",
            new=AsyncMock(),
        ),
        "create_redis_client": patch(
            "interlock.gateway.app.create_redis_client",
            new=AsyncMock(return_value=mock_redis),
        ),
        "close_redis_client": patch(
            "interlock.gateway.app.close_redis_client",
            new=AsyncMock(),
        ),
        "pg_proxy_start": patch(
            "interlock.gateway.app.PGProxy.start",
            new=AsyncMock(),
        ),
        "pg_proxy_stop": patch(
            "interlock.gateway.app.PGProxy.stop",
            new=AsyncMock(),
        ),
        "registry_load": patch(
            "interlock.gateway.app.MetadataRegistry.load",
            new=AsyncMock(),
        ),
    }

    return patches, mock_pool, mock_redis


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateApp:
    def test_returns_starlette(self) -> None:
        app = create_app(config=_make_config())
        assert isinstance(app, Starlette)

    def test_config_stored_on_state(self) -> None:
        cfg = _make_config()
        app = create_app(config=cfg)
        assert app.state.config is cfg

    def test_observability_bootstrap_is_wired(self) -> None:
        cfg = _make_config()
        otel_result = MagicMock()

        with patch(
            "interlock.gateway.app.configure_service_otel", return_value=otel_result
        ) as bootstrap:
            app = create_app(config=cfg)

        bootstrap.assert_called_once_with(
            service_name="interlock-gateway",
            config=cfg,
            app=app,
        )
        assert app.state.otel is otel_result

    def test_rejects_unverified_control_db_tls_in_production(self) -> None:
        cfg = _make_config()
        cfg.environment = "production"
        cfg.database.ssl_mode = "disable"
        cfg.database.ssl_ca_file = None

        with pytest.raises(ConfigValidationError, match="database.ssl_mode"):
            create_app(config=cfg)


@pytest.mark.asyncio
class TestHealthEndpoint:
    async def test_health_returns_ok(self) -> None:
        patches, _pool, _redis = _patch_infrastructure()
        cfg = _make_config()
        app = create_app(config=cfg)

        with (
            patches["create_pg_pool"],
            patches["close_pg_pool"],
            patches["create_redis_client"],
            patches["close_redis_client"],
            patches["pg_proxy_start"],
            patches["pg_proxy_stop"],
            patches["registry_load"],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "gateway"
        assert "active_connections" in body

    async def test_health_active_connections_is_int(self) -> None:
        patches, _pool, _redis = _patch_infrastructure()
        cfg = _make_config()
        app = create_app(config=cfg)

        with (
            patches["create_pg_pool"],
            patches["close_pg_pool"],
            patches["create_redis_client"],
            patches["close_redis_client"],
            patches["pg_proxy_start"],
            patches["pg_proxy_stop"],
            patches["registry_load"],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health")

        assert isinstance(resp.json()["active_connections"], int)


@pytest.mark.asyncio
class TestReadyEndpoint:
    async def test_ready_returns_dependency_status(self) -> None:
        cfg = _make_config()
        app = create_app(config=cfg)
        migration_rows = [
            {"version": migration.version, "checksum": migration.checksum}
            for migration in discover_migrations(ROOT / "migrations")
        ]
        app.state.pg_pool = SimpleNamespace(
            fetchval=AsyncMock(return_value=1),
            fetch=AsyncMock(return_value=migration_rows),
        )
        app.state.redis_client = SimpleNamespace(ping=AsyncMock(return_value=True))
        app.state.registry = SimpleNamespace(get_all=lambda: [object()])
        app.state.pg_proxy = SimpleNamespace(active_connections=2)
        app.state.audit_buffer = SimpleNamespace(
            health=lambda: SimpleNamespace(
                degraded=False,
                queue_depth=0,
                spool_pending=0,
                dlq_count=0,
                has_dead_letters=False,
            )
        )
        app.state.cache_invalidator = SimpleNamespace(listener_healthy=True)
        app.state.faiss_sync = SimpleNamespace(healthy=True)
        app.state.discovery_index = SimpleNamespace(
            freshness=AsyncMock(
                return_value={"loaded_generation": 3, "remote_generation": 3, "fresh": True}
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready", headers={"x-request-id": "gateway-ready-1"})

        assert resp.status_code == 200
        assert resp.headers["x-correlation-id"] == "gateway-ready-1"
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["postgres"]["status"] == "ok"
        assert body["checks"]["redis"]["status"] == "ok"
        assert body["checks"]["registry"]["sources"] == 1
        assert body["checks"]["pg_proxy"]["active_connections"] == 2
        assert body["checks"]["audit"]["status"] == "ok"
        assert body["checks"]["discovery_vector"]["fresh"] is True

    async def test_ready_returns_503_when_dependency_missing(self) -> None:
        cfg = _make_config()
        app = create_app(config=cfg)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ready")

        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"


@pytest.mark.asyncio
class TestStatsEndpoint:
    async def test_stats_requires_bearer_by_default(self) -> None:
        cfg = _make_config()
        app = create_app(config=cfg)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/stats")

        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
        assert resp.json()["error"] == "Authentication required"

    async def test_stats_returns_structure_when_public_stats_enabled(self) -> None:
        patches, _pool, _redis = _patch_infrastructure()
        cfg = _make_config()
        cfg.observability.public_stats_enabled = True
        app = create_app(config=cfg)

        with (
            patches["create_pg_pool"],
            patches["close_pg_pool"],
            patches["create_redis_client"],
            patches["close_redis_client"],
            patches["pg_proxy_start"],
            patches["pg_proxy_stop"],
            patches["registry_load"],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/stats")

        assert resp.status_code == 200
        body = resp.json()
        assert "cache" in body
        assert "l1" in body["cache"]
        assert "l2" in body["cache"]
        assert "active_connections" in body
        assert "registry_sources" in body
        assert isinstance(body["registry_sources"], int)

    async def test_stats_returns_structure_with_authenticated_bearer(self) -> None:
        cfg = _make_config()
        app = create_app(config=cfg)
        app.state.auth_manager = SimpleNamespace(authenticate=AsyncMock(return_value=object()))
        app.state.l1_cache = SimpleNamespace(stats={"hits": 1})
        app.state.l2_cache = SimpleNamespace(stats={"hits": 2})
        app.state.pg_proxy = SimpleNamespace(active_connections=3)
        app.state.registry = SimpleNamespace(get_all=lambda: [object(), object()])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/stats", headers={"authorization": "Bearer test-key"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["cache"]["l1"] == {"hits": 1}
        assert body["cache"]["l2"] == {"hits": 2}
        assert body["active_connections"] == 3
        assert body["registry_sources"] == 2
        app.state.auth_manager.authenticate.assert_awaited_once_with("test-key")
