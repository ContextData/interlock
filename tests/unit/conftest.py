"""Unit-test defaults.

Connector activation lives in the control database (migration 018). Unit
tests run against fake pools that know nothing about that table, so every
activatable connector is treated as active unless a test opts out with
`@pytest.mark.real_connector_activation` to exercise the real lookup.
"""

from __future__ import annotations

from typing import Any

import pytest

from interlock.admin.routes import dashboard
from interlock.connections import activation
from interlock.connections.connectors import CONNECTOR_DEFINITIONS


@pytest.fixture(autouse=True)
def _every_connector_active(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("real_connector_activation"):
        return
    keys = frozenset(key for key in CONNECTOR_DEFINITIONS if activation.activatable(key))

    async def _all_active(_pool: Any) -> frozenset[str]:
        return keys

    monkeypatch.setattr(activation, "active_connector_keys", _all_active)
    monkeypatch.setattr(dashboard, "active_connector_keys", _all_active)
