"""Regression test for audit P1-C: PG proxy routes via Metadata Registry.

AUDIT-COVERS: P1-C

The audit reported that PGProxy opened a fixed upstream connection from
config (``pg_proxy.py:178-185``) and ignored ConnectionManager / the
Metadata Registry. In Docker Compose this caused the proxy to serve the
control database instead of any registered customer source.

Phase 1 introduces optional registry-backed routing: when a registry is
provided, the proxy resolves the upstream from the data source named by
the connection's ``database`` parameter. Unknown databases get a typed
FATAL error instead of silently falling through to the default upstream.
"""

from __future__ import annotations

import inspect

from interlock.gateway.pg_proxy import PGProxy, _extract_startup_params


def test_p1_c_proxy_init_accepts_registry() -> None:
    sig = inspect.signature(PGProxy.__init__)
    assert "registry" in sig.parameters, "PGProxy must accept a registry kwarg (P1-C regression)"


def test_p1_c_handle_client_resolves_database_via_registry() -> None:
    src = inspect.getsource(PGProxy._handle_client)
    assert (
        "self._registry.get(startup_db)" in src
    ), "PGProxy must resolve the connection database via the registry"
    assert (
        "Unknown data source" in src
    ), "Unknown databases must be rejected with a typed FATAL error"


def test_p1_c_extract_startup_params_returns_user_and_database() -> None:
    """Build a synthetic startup message and assert both keys are parsed."""
    body = b""
    for k, v in (("user", "alice"), ("database", "analytics")):
        body += k.encode() + b"\x00" + v.encode() + b"\x00"
    body += b"\x00"
    # 4-byte length + 4-byte version + body
    import struct

    msg = struct.pack(">I", 8 + len(body)) + struct.pack(">I", 196608) + body
    params = _extract_startup_params(msg)
    assert params["user"] == "alice"
    assert params["database"] == "analytics"


def test_p1_c_gateway_app_wires_registry_into_pg_proxy() -> None:
    from interlock.gateway import app as gateway_app

    src = inspect.getsource(gateway_app.lifespan)
    assert "registry=registry" in src, (
        "Gateway lifespan must pass registry= into PGProxy(...) " "(P1-C regression)"
    )
