"""Gateway cache invalidation lifecycle wiring tests."""

from __future__ import annotations

import inspect

from interlock.gateway import app as gateway_app


def test_gateway_lifespan_starts_and_stops_cache_invalidation_listener() -> None:
    src = inspect.getsource(gateway_app.lifespan)

    assert "CacheInvalidator(" in src
    assert "pg_pool=pg_pool" in src
    assert "instance_id=instance_manager.instance_id" in src
    assert "await cache_invalidator.start_listener()" in src
    assert "await cache_invalidator.stop_listener()" in src
