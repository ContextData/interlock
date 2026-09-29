from __future__ import annotations

import json

import pytest

from interlock.connections.connectors import get_adapter
from interlock.core.source_roles import SourceRoleEvaluator


class FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


class FakeSlackResponse(dict):
    pass


class FakeSdkSlackResponse:
    def __init__(self, data: dict) -> None:
        self.data = data


class FakeSlackClient:
    async def auth_test(self):
        return FakeSlackResponse({"ok": True, "team": "Acme"})

    async def conversations_list(self, **_kwargs):
        return FakeSlackResponse(
            {
                "channels": [
                    {
                        "id": "C123",
                        "name": "eng-runbooks",
                        "is_private": False,
                    }
                ]
            }
        )

    async def conversations_info(self, channel: str):
        return FakeSdkSlackResponse(
            {
                "channel": {
                    "id": channel,
                    "name": "eng-runbooks" if channel == "C123" else "other",
                    "is_private": False,
                }
            }
        )

    async def conversations_history(self, channel: str, **kwargs):
        if kwargs.get("latest") == "1000.0001":
            return FakeSlackResponse(
                {
                    "messages": [
                        {
                            "ts": "1000.0001",
                            "text": "Deploy checklist",
                            "user": "U1",
                        }
                    ]
                }
            )
        return FakeSlackResponse(
            {
                "messages": [
                    {
                        "ts": "1000.0001",
                        "thread_ts": "1000.0001",
                        "reply_count": 1,
                        "text": "Deploy checklist",
                        "user": "U1",
                        "files": [
                            {
                                "id": "F123",
                                "title": "runbook.md",
                                "filetype": "markdown",
                                "mimetype": "text/markdown",
                                "size": 42,
                                "user": "U1",
                            }
                        ],
                    }
                ]
            }
        )

    async def conversations_replies(self, channel: str, ts: str, **kwargs):
        if kwargs.get("latest") == "1000.0002":
            return FakeSlackResponse(
                {
                    "messages": [
                        {
                            "ts": "1000.0002",
                            "thread_ts": "1000.0001",
                            "text": "Rollback complete",
                            "user": "U2",
                        }
                    ]
                }
            )
        return FakeSlackResponse(
            {
                "messages": [
                    {
                        "ts": "1000.0001",
                        "thread_ts": "1000.0001",
                        "text": "Deploy checklist",
                        "user": "U1",
                    },
                    {
                        "ts": "1000.0002",
                        "thread_ts": "1000.0001",
                        "text": "Rollback complete",
                        "user": "U2",
                    },
                ]
            }
        )

    async def files_list(self, **_kwargs):
        return FakeSlackResponse(
            {
                "files": [
                    {
                        "id": "F999",
                        "title": "incident.pdf",
                        "filetype": "pdf",
                        "mimetype": "application/pdf",
                        "size": 100,
                        "channels": ["C123"],
                        "user": "U3",
                    }
                ]
            }
        )

    async def files_info(self, file: str):
        return FakeSlackResponse({"file": {"id": file, "title": "runbook.md"}})


SLACK_CONFIG = {
    "workspace": "acme",
    "bot_token": "xoxb-test",
    "channel_types": "public_channel,private_channel",
    "include_messages": True,
    "include_threads": True,
    "include_files": True,
}


@pytest.mark.asyncio
async def test_slack_probe_and_asset_listing(monkeypatch) -> None:
    monkeypatch.setattr(
        "interlock.connections.connectors._slack_client", lambda _config: FakeSlackClient()
    )
    adapter = get_adapter("slack", {"connector_key": "slack"})

    probe = await adapter.probe(SLACK_CONFIG)
    assets = await adapter.list_assets(SLACK_CONFIG)

    assert probe.healthy is True
    assert [asset["asset_path"] for asset in assets] == [
        "slack://channel/C123",
        "slack://channel/C123/message/1000.0001",
        "slack://file/F123",
        "slack://channel/C123/thread/1000.0001/message/1000.0002",
        "slack://file/F999",
    ]
    channel = assets[0]
    assert channel["metadata"]["channel_name"] == "eng-runbooks"
    assert channel["metadata"]["channel_type"] == "public_channel"
    message = assets[1]
    assert message["metadata"]["user_id"] == "U1"
    assert message["metadata"]["workspace"] == "acme"
    file_asset = assets[2]
    assert file_asset["metadata"]["file_type"] == "markdown"
    assert file_asset["metadata"]["mimetype"] == "text/markdown"


@pytest.mark.asyncio
async def test_slack_asset_listing_can_be_channel_scoped(monkeypatch) -> None:
    monkeypatch.setattr(
        "interlock.connections.connectors._slack_client", lambda _config: FakeSlackClient()
    )
    adapter = get_adapter("slack", {"connector_key": "slack"})

    assets = await adapter.list_assets(
        {
            **SLACK_CONFIG,
            "channel_ids": "C123",
        }
    )

    assert "slack://channel/C123" in {asset["asset_path"] for asset in assets}
    assert all(asset["metadata"].get("channel_id") in {None, "C123"} for asset in assets)


@pytest.mark.asyncio
async def test_slack_fetch_message_thread_and_file(monkeypatch) -> None:
    monkeypatch.setattr(
        "interlock.connections.connectors._slack_client", lambda _config: FakeSlackClient()
    )
    adapter = get_adapter("slack", {"connector_key": "slack"})

    message = json.loads(
        (await adapter.fetch_asset(SLACK_CONFIG, "slack://channel/C123/message/1000.0001")).decode()
    )
    reply = json.loads(
        (
            await adapter.fetch_asset(
                SLACK_CONFIG,
                "slack://channel/C123/thread/1000.0001/message/1000.0002",
            )
        ).decode()
    )
    file_payload = json.loads(
        (await adapter.fetch_asset(SLACK_CONFIG, "slack://file/F123")).decode()
    )

    assert message["messages"][0]["text"] == "Deploy checklist"
    assert reply["messages"][0]["text"] == "Rollback complete"
    assert file_payload["file"]["id"] == "F123"


def test_slack_permission_request_is_collaboration_scoped() -> None:
    adapter = get_adapter("slack", {"connector_key": "slack"})

    channel = adapter.build_permission_request(
        source_id="slack",
        identity_id=10,
        operation="channel",
        metadata={"asset_ref": "slack://channel/C123", "channel_name": "eng-runbooks"},
    )
    message = adapter.build_permission_request(
        source_id="slack",
        identity_id=10,
        operation="message",
        metadata={"asset_ref": "slack://channel/C123/message/1000.0001", "user_id": "U1"},
    )
    file_request = adapter.build_permission_request(
        source_id="slack",
        identity_id=10,
        operation="file",
        metadata={"asset_ref": "slack://file/F123", "file_type": "markdown"},
    )

    assert channel.action == "collab.channel.read"
    assert channel.metadata["channel_id"] == "C123"
    assert message.action == "collab.message.read"
    assert message.metadata["ts"] == "1000.0001"
    assert file_request.action == "collab.file.read"
    assert file_request.metadata["file_id"] == "F123"


@pytest.mark.asyncio
async def test_slack_role_constraints_match_channel_user_and_file_type() -> None:
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                {
                    "permission_id": 1,
                    "role_id": 1,
                    "role_key": "channel_reader",
                    "effect": "allow",
                    "action": "collab.message.read",
                    "resource_type": "collab.message",
                    "resource_pattern": "*",
                    "constraints": {
                        "workspaces": ["acme"],
                        "channel_ids": ["C123"],
                        "channel_types": ["public_channel"],
                        "user_ids": ["U1"],
                    },
                },
                {
                    "permission_id": 2,
                    "role_id": 2,
                    "role_key": "compliance_reader",
                    "effect": "allow",
                    "action": "collab.file.read",
                    "resource_type": "collab.file",
                    "resource_pattern": "*",
                    "constraints": {"file_types": ["markdown"]},
                },
            ]
        )
    )
    adapter = get_adapter("slack", {"connector_key": "slack"})

    allowed_message = await evaluator.evaluate(
        adapter.build_permission_request(
            source_id="slack",
            identity_id=10,
            operation="message",
            metadata={
                "asset_ref": "slack://channel/C123/message/1000.0001",
                "workspace": "acme",
                "channel_type": "public_channel",
                "user_id": "U1",
            },
        )
    )
    denied_message = await evaluator.evaluate(
        adapter.build_permission_request(
            source_id="slack",
            identity_id=10,
            operation="message",
            metadata={
                "asset_ref": "slack://channel/C999/message/1000.0001",
                "workspace": "acme",
                "channel_type": "public_channel",
                "user_id": "U1",
            },
        )
    )
    allowed_file = await evaluator.evaluate(
        adapter.build_permission_request(
            source_id="slack",
            identity_id=10,
            operation="file",
            metadata={"asset_ref": "slack://file/F123", "file_type": "markdown"},
        )
    )

    assert allowed_message.allowed is True
    assert denied_message.allowed is False
    assert allowed_file.allowed is True
