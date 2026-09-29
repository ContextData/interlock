"""Regression test for audit P0-E: DiscoverySearch wired into gateway.

AUDIT-COVERS: P0-E

The audit found that ``MCPAdapter._discover()`` reads
``request.app.state.discovery_search`` but gateway lifespan never
created or assigned that instance. The fix: gateway lifespan now
constructs an EmbeddingEngine and a DiscoverySearch and stores both on
app.state so MCP discovery is reachable.

Because lifespan needs PG/Redis, this test directly inspects the
lifespan source to assert the wiring is present, complemented by an
e2e test that exercises a real MCP discover call against a running
stack.
"""

from __future__ import annotations

import inspect

from interlock.discovery.search import DiscoverySearch
from interlock.gateway import app as gateway_app


def test_p0_e_lifespan_constructs_discovery_search() -> None:
    src = inspect.getsource(gateway_app.lifespan)
    assert (
        "DiscoverySearch(" in src
    ), "Gateway lifespan must construct DiscoverySearch (P0-E regression)"
    assert (
        "app.state.discovery_search" in src
    ), "DiscoverySearch must be stored on app.state.discovery_search"


def test_p0_e_lifespan_constructs_embedding_engine() -> None:
    src = inspect.getsource(gateway_app.lifespan)
    assert "EmbeddingEngine(" in src, (
        "Gateway lifespan must construct EmbeddingEngine for both "
        "semantic cache and discovery (P0-E + P1-D regression)"
    )
    assert "app.state.embedding_engine" in src


def test_p0_e_discovery_search_accepts_gateway_signature() -> None:
    sig = inspect.signature(DiscoverySearch.__init__)
    params = set(sig.parameters)
    assert {"pg_pool", "semantic_index", "embedding_engine"} <= params
