"""Optional live Slack connector certification checks."""

from __future__ import annotations

import os

import pytest

from interlock.connections.connectors import get_adapter


def _slack_config() -> dict[str, str]:
    return {
        "workspace": os.environ.get("SLACK_WORKSPACE", ""),
        "bot_token": os.environ.get("SLACK_BOT_TOKEN", ""),
        "bot_token_ref": os.environ.get("SLACK_BOT_TOKEN_REF", ""),
        "channel_types": os.environ.get(
            "SLACK_CHANNEL_TYPES",
            "public_channel,private_channel",
        ),
        "include_messages": os.environ.get("SLACK_INCLUDE_MESSAGES", "true"),
        "include_threads": os.environ.get("SLACK_INCLUDE_THREADS", "true"),
        "include_files": os.environ.get("SLACK_INCLUDE_FILES", "true"),
        "max_channels": os.environ.get("SLACK_MAX_CHANNELS", "5"),
        "max_messages_per_channel": os.environ.get("SLACK_MAX_MESSAGES_PER_CHANNEL", "5"),
        "max_replies_per_thread": os.environ.get("SLACK_MAX_REPLIES_PER_THREAD", "5"),
        "max_files": os.environ.get("SLACK_MAX_FILES", "5"),
    }


def _has_live_slack_config() -> bool:
    cfg = _slack_config()
    return bool(cfg["workspace"] and (cfg["bot_token"] or cfg["bot_token_ref"]))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.skipif(
        not _has_live_slack_config(),
        reason="Set SLACK_WORKSPACE and SLACK_BOT_TOKEN or SLACK_BOT_TOKEN_REF to run live Slack tests.",
    ),
]


@pytest.mark.asyncio
async def test_live_slack_probe_introspect_and_list_assets() -> None:
    adapter = get_adapter("slack", {"connector_key": "slack"})
    cfg = _slack_config()

    probe = await adapter.probe(cfg)
    assert probe.healthy is True, probe.error

    metadata = await adapter.introspect(cfg)
    assert metadata["connector"] == "slack"
    assert {obj["object_type"] for obj in metadata["objects"]} >= {"channel", "message", "file"}

    assets = await adapter.list_assets(cfg)
    assert isinstance(assets, list)
    for asset in assets:
        assert asset["asset_path"].startswith("slack://")
        assert asset["metadata"]["connector_key"] == "slack"
