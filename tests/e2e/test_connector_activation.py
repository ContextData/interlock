"""A deactivated connector is hidden and refused everywhere, then restored.

The stack seeds every connector active; this proves the switch against the
real control database and leaves it as it found it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = [pytest.mark.e2e]

_KEY = "zendesk"


@pytest.mark.asyncio
async def test_deactivating_a_connector_hides_and_refuses_it(
    admin_session: Any, control_db: Any
) -> None:
    headers = {"X-CSRF-Token": admin_session.csrf_token}
    client = admin_session.client
    try:
        off = client.post(
            f"/dashboard/connectors/{_KEY}/activation", data={"active": ""}, headers=headers
        )
        assert off.status_code == 200, off.text[:300]
        assert (
            await control_db.fetchval(
                "SELECT active FROM connector_activation WHERE connector_key = $1", _KEY
            )
            is False
        )

        for url in ("/dashboard/data-sources/new", "/dashboard/source-wizard"):
            assert f'option value="{_KEY}"' not in client.get(url).text, url

        created = client.post(
            "/api/data-sources",
            json={
                "source_id": "e2e_inactive_connector",
                "name": "inactive",
                "source_type": "saas",
                "connector_key": _KEY,
                "connection_config": {"subdomain": "example"},
            },
            headers=headers,
        )
        assert created.status_code == 422
        assert "is not active" in created.text
        assert (
            await control_db.fetchval(
                "SELECT 1 FROM data_sources WHERE source_id = 'e2e_inactive_connector'"
            )
            is None
        )

        audit = await control_db.fetchval(
            "SELECT action FROM admin_audit_log WHERE resource = 'connector' "
            "AND resource_id = $1 ORDER BY id DESC LIMIT 1",
            _KEY,
        )
        assert audit == "connector.deactivate"
    finally:
        on = client.post(
            f"/dashboard/connectors/{_KEY}/activation", data={"active": "on"}, headers=headers
        )
        assert on.status_code == 200
    assert f'option value="{_KEY}"' in client.get("/dashboard/data-sources/new").text
