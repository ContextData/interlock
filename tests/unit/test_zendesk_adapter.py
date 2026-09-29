from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from interlock.connections.connectors import get_adapter
from interlock.core.source_roles import SourceRoleEvaluator


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeZendeskClient:
    calls: list[str] = []

    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> FakeZendeskClient:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        path = urlparse(url).path
        self.calls.append(url)
        if path == "/api/v2/users/me.json":
            return FakeResponse({"user": {"id": 1, "name": "InterLock"}})
        if path == "/api/v2/tickets.json":
            return FakeResponse(
                {
                    "tickets": [
                        {
                            "id": 101,
                            "subject": "Cannot login",
                            "updated_at": "2026-05-01T10:00:00Z",
                            "brand_id": 10,
                            "group_id": 20,
                            "organization_id": 30,
                            "status": "open",
                            "tags": ["vip", "login"],
                        }
                    ]
                }
            )
        if path == "/api/v2/help_center/articles.json":
            return FakeResponse(
                {
                    "articles": [
                        {
                            "id": 202,
                            "title": "Reset password",
                            "updated_at": "2026-05-02T10:00:00Z",
                            "section_id": 40,
                            "locale": "en-us",
                        }
                    ]
                }
            )
        if path == "/api/v2/users.json":
            return FakeResponse(
                {
                    "users": [
                        {
                            "id": 303,
                            "name": "Ada",
                            "updated_at": "2026-05-03T10:00:00Z",
                            "organization_id": 30,
                            "role": "end-user",
                        }
                    ]
                }
            )
        if path == "/api/v2/organizations.json":
            return FakeResponse(
                {
                    "organizations": [
                        {
                            "id": 404,
                            "name": "Acme",
                            "updated_at": "2026-05-04T10:00:00Z",
                            "tags": ["enterprise"],
                        }
                    ]
                }
            )
        if path == "/api/v2/tickets/101.json":
            return FakeResponse({"ticket": {"id": 101, "subject": "Cannot login"}})
        if path == "/api/v2/tickets/101/comments.json":
            return FakeResponse(
                {
                    "comments": [{"id": 1, "body": "Customer email is ada@example.com"}],
                    "users": [{"id": 303, "name": "Ada"}],
                }
            )
        if path == "/api/v2/incremental/tickets/cursor.json":
            return FakeResponse(
                {
                    "tickets": [
                        {"id": 505, "subject": "Billing", "updated_at": "2026-05-05T10:00:00Z"}
                    ],
                    "end_time": 1_778_000_000,
                    "after_cursor": "next-cursor",
                }
            )
        return FakeResponse({}, status_code=404)


class FakeZendeskProbeFailureClient:
    status_code = 401

    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "FakeZendeskProbeFailureClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse({}, status_code=self.status_code)


class FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


ZENDESK_CONFIG = {
    "subdomain": "acme",
    "email": "interlock@example.com",
    "api_token": "token",
    "brand_id": "10",
    "group_id": "20",
    "organization_id": "30",
    "status": "open",
    "allow_private_egress": True,
}


@pytest.mark.asyncio
async def test_zendesk_probe_and_asset_listing(monkeypatch) -> None:
    FakeZendeskClient.calls = []
    monkeypatch.setattr("httpx.AsyncClient", FakeZendeskClient)
    adapter = get_adapter("zendesk", {"connector_key": "zendesk"})

    probe = await adapter.probe(ZENDESK_CONFIG)
    assets = await adapter.list_assets(ZENDESK_CONFIG)

    assert probe.healthy is True
    assert [asset["asset_path"] for asset in assets] == [
        "zendesk://ticket/101",
        "zendesk://article/202",
        "zendesk://user/303",
        "zendesk://organization/404",
    ]
    ticket = assets[0]
    assert ticket["metadata"]["brand_id"] == 10
    assert ticket["metadata"]["group_id"] == 20
    assert ticket["metadata"]["organization_id"] == 30
    assert ticket["metadata"]["tags"] == ["vip", "login"]
    assert any("brand_id=10" in call for call in FakeZendeskClient.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
async def test_zendesk_probe_401_403_429_500_are_unhealthy(monkeypatch, status_code: int) -> None:
    FakeZendeskProbeFailureClient.status_code = status_code
    monkeypatch.setattr("httpx.AsyncClient", FakeZendeskProbeFailureClient)
    adapter = get_adapter("zendesk", {"connector_key": "zendesk"})

    probe = await adapter.probe(ZENDESK_CONFIG)

    assert probe.healthy is False
    assert probe.error == f"HTTP {status_code}"


@pytest.mark.asyncio
async def test_zendesk_fetch_ticket_includes_comments(monkeypatch) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakeZendeskClient)
    adapter = get_adapter("zendesk", {"connector_key": "zendesk"})

    payload = json.loads(
        (await adapter.fetch_asset(ZENDESK_CONFIG, "zendesk://ticket/101")).decode()
    )

    assert payload["ticket"]["subject"] == "Cannot login"
    assert payload["comments"][0]["body"] == "Customer email is ada@example.com"
    assert payload["comment_users"][0]["name"] == "Ada"


@pytest.mark.asyncio
async def test_zendesk_incremental_sync_returns_ticket_assets(monkeypatch) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakeZendeskClient)
    adapter = get_adapter("zendesk", {"connector_key": "zendesk"})

    result = await adapter.sync_incremental(ZENDESK_CONFIG, {"start_time": 0})

    assert result["state"] == {"start_time": 1_778_000_000, "cursor": "next-cursor"}
    assert result["assets"][0]["asset_path"] == "zendesk://ticket/505"
    assert result["assets"][0]["metadata"]["object_type"] == "ticket"


def test_zendesk_permission_request_is_object_scoped() -> None:
    adapter = get_adapter("zendesk", {"connector_key": "zendesk"})

    read_request = adapter.build_permission_request(
        source_id="zd",
        identity_id=10,
        operation="read",
        metadata={
            "object_type": "ticket",
            "object_id": "101",
            "brand_id": "10",
            "group_id": "20",
            "status": "open",
            "tags": ["vip"],
        },
    )
    write_request = adapter.build_permission_request(
        source_id="zd",
        identity_id=10,
        operation="comment",
        metadata={"object_type": "ticket", "object_id": "101"},
    )

    assert read_request.action == "saas.object.read"
    assert read_request.resources == ["zendesk:ticket/101"]
    assert read_request.metadata["group_id"] == "20"
    assert write_request.action == "saas.object.write"


@pytest.mark.asyncio
async def test_zendesk_role_constraints_match_ticket_metadata() -> None:
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                {
                    "permission_id": 1,
                    "role_id": 1,
                    "role_key": "support_reader",
                    "effect": "allow",
                    "action": "saas.object.read",
                    "resource_type": "saas.object",
                    "resource_pattern": "zendesk:ticket/*",
                    "constraints": {
                        "object_types": ["ticket"],
                        "group_ids": ["20"],
                        "statuses": ["open"],
                        "tags": ["vip"],
                    },
                }
            ]
        )
    )
    adapter = get_adapter("zendesk", {"connector_key": "zendesk"})

    allowed = await evaluator.evaluate(
        adapter.build_permission_request(
            source_id="zd",
            identity_id=10,
            operation="read",
            metadata={
                "object_type": "ticket",
                "object_id": "101",
                "group_id": "20",
                "status": "open",
                "tags": ["vip", "login"],
            },
        )
    )
    denied = await evaluator.evaluate(
        adapter.build_permission_request(
            source_id="zd",
            identity_id=10,
            operation="read",
            metadata={
                "object_type": "ticket",
                "object_id": "101",
                "group_id": "99",
                "status": "open",
                "tags": ["vip"],
            },
        )
    )

    assert allowed.allowed is True
    assert denied.allowed is False
