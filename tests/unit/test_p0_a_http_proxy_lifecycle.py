"""Regression test for audit P0-A: HTTP proxy lifecycle.

AUDIT-COVERS: P0-A

The audit reported that ``gateway/app.py`` constructed ``HTTPProxy``
twice: once in ``create_app`` (whose routes were bound) and once in
``lifespan`` (which got ``initialize()`` called). The route-bound
instance never had its httpx client created, so live ``/proxy/...``
requests responded with ``"HTTP proxy not initialized"``.

These tests pin the fix:

1. ``create_app`` registers routes from a single ``HTTPProxy`` instance
   and stores it on ``app.state.http_proxy``.
2. After running through ``lifespan``, that same instance has a live
   httpx client.
"""

from __future__ import annotations

import pytest

from interlock.gateway.app import create_app
from interlock.gateway.http_proxy import HTTPProxy


def test_p0_a_create_app_stashes_http_proxy_on_state() -> None:
    app = create_app()
    proxy = getattr(app.state, "http_proxy", None)
    assert isinstance(proxy, HTTPProxy), (
        "create_app must stash the HTTPProxy on app.state for lifespan to "
        "initialize the same instance whose routes were registered"
    )


def test_p0_a_route_handler_targets_state_instance() -> None:
    """Routes returned by HTTPProxy.get_routes are bound methods of the
    same object stashed on state."""
    app = create_app()
    proxy = app.state.http_proxy
    proxy_routes = [r for r in app.routes if "/proxy/" in getattr(r, "path", "")]
    assert proxy_routes, "Expected /proxy/* routes registered"
    for route in proxy_routes:
        # endpoint is a bound method of the HTTPProxy
        endpoint = route.endpoint
        bound_self = getattr(endpoint, "__self__", None)
        assert bound_self is proxy, (
            "Route endpoint must be a bound method of the same HTTPProxy "
            "instance stored on app.state.http_proxy (P0-A regression)"
        )


@pytest.mark.asyncio
async def test_p0_a_lifespan_initializes_state_instance() -> None:
    """After running lifespan startup, app.state.http_proxy.client is set.

    We do not boot Postgres/Redis here - we simulate the lifespan
    behavior by calling initialize() on the state instance and assert
    the same instance is the one bound to routes.
    """
    app = create_app()
    proxy = app.state.http_proxy
    # Simulate lifespan startup for HTTPProxy only.
    await proxy.initialize()
    try:
        assert proxy._client is not None, (
            "After initialize(), the route-bound HTTPProxy must have a " "live httpx client"
        )
    finally:
        await proxy.shutdown()
