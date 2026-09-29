"""A source registered without an id gets one generated from its display name."""

from __future__ import annotations

import secrets
from typing import Any

import pytest

pytestmark = [pytest.mark.e2e]


@pytest.mark.asyncio
async def test_api_registration_without_an_id_generates_one(
    admin_session: Any, control_db: Any
) -> None:
    token = secrets.token_hex(3)
    headers = {"X-CSRF-Token": admin_session.csrf_token}
    body = {
        "name": f"E2E Generated {token}",
        "source_type": "http",
        "connector_key": "generic_rest",
        "connection_config": {"base_url": "https://example.com"},
    }
    first = admin_session.client.post("/api/data-sources", json=body, headers=headers)
    second = admin_session.client.post("/api/data-sources", json=body, headers=headers)
    created = [first.json().get("source_id"), second.json().get("source_id")]
    try:
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert created == [f"e2e_generated_{token}", f"e2e_generated_{token}_2"]
        page = admin_session.client.get(f"/dashboard/data-sources/{created[0]}")
        assert page.status_code == 200
        assert f"<h1>E2E Generated {token}</h1>" in page.text
        assert f'data-copy-text="{created[0]}"' in page.text
    finally:
        for source_id in created:
            if source_id:
                await control_db.execute("DELETE FROM data_sources WHERE source_id = $1", source_id)
