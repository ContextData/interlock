"""Optional live Zendesk connector checks.

These tests are skipped unless explicit Zendesk credentials are provided in the
environment. They are read-only certification checks for temporary live creds.
"""

from __future__ import annotations

import os

import pytest

from interlock.connections.connectors import get_adapter


def _zendesk_config() -> dict[str, str]:
    return {
        "base_url": os.environ.get("ZENDESK_BASE_URL", ""),
        "subdomain": os.environ.get("ZENDESK_SUBDOMAIN", ""),
        "email": os.environ.get("ZENDESK_EMAIL", ""),
        "api_token": os.environ.get("ZENDESK_API_TOKEN", ""),
        "api_token_ref": os.environ.get("ZENDESK_API_TOKEN_REF", ""),
        "brand_id": os.environ.get("ZENDESK_BRAND_ID", ""),
        "group_id": os.environ.get("ZENDESK_GROUP_ID", ""),
        "organization_id": os.environ.get("ZENDESK_ORGANIZATION_ID", ""),
        "status": os.environ.get("ZENDESK_TICKET_STATUS", ""),
        "max_tickets": os.environ.get("ZENDESK_MAX_TICKETS", "5"),
        "max_articles": os.environ.get("ZENDESK_MAX_ARTICLES", "5"),
        "max_users": os.environ.get("ZENDESK_MAX_USERS", "0"),
        "max_organizations": os.environ.get("ZENDESK_MAX_ORGANIZATIONS", "0"),
        "include_users": os.environ.get("ZENDESK_INCLUDE_USERS", "false"),
        "include_organizations": os.environ.get("ZENDESK_INCLUDE_ORGANIZATIONS", "false"),
        "include_ticket_comments": os.environ.get("ZENDESK_INCLUDE_TICKET_COMMENTS", "false"),
    }


def _has_live_zendesk_config() -> bool:
    cfg = _zendesk_config()
    has_base = bool(cfg["base_url"] or cfg["subdomain"])
    has_token = bool(cfg["api_token"] or cfg["api_token_ref"])
    return has_base and bool(cfg["email"]) and has_token


pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.skipif(
        not _has_live_zendesk_config(),
        reason="Set ZENDESK_SUBDOMAIN or ZENDESK_BASE_URL, ZENDESK_EMAIL, and ZENDESK_API_TOKEN or ZENDESK_API_TOKEN_REF to run live Zendesk tests.",
    ),
]


@pytest.mark.asyncio
async def test_live_zendesk_probe_and_list_assets() -> None:
    adapter = get_adapter("zendesk", {"connector_key": "zendesk"})
    cfg = _zendesk_config()

    probe = await adapter.probe(cfg)
    assert probe.healthy is True, probe.error

    metadata = await adapter.introspect(cfg)
    assert metadata["connector"] == "zendesk"
    assert {obj["object_type"] for obj in metadata["objects"]} >= {"ticket", "article"}

    assets = await adapter.list_assets(cfg)
    assert isinstance(assets, list)
    for asset in assets:
        assert asset["asset_path"].startswith("zendesk://")
        assert asset["metadata"]["connector_key"] == "zendesk"
