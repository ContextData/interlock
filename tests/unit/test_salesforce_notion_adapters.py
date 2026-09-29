from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from interlock.connections.connectors import get_adapter


class FakeSalesforceObject:
    def __init__(self, object_name: str) -> None:
        self.object_name = object_name

    def describe(self) -> dict:
        return {
            "name": self.object_name,
            "fields": [
                {"name": "Id", "type": "id"},
                {"name": "Name", "type": "string"},
                {"name": "LastModifiedDate", "type": "datetime"},
            ],
        }

    def get(self, object_id: str) -> dict:
        return {"Id": object_id, "Name": f"{self.object_name} {object_id}"}


class FakeSalesforce:
    def __getattr__(self, object_name: str) -> FakeSalesforceObject:
        return FakeSalesforceObject(object_name)

    def query_all(self, query: str) -> dict:
        object_name = query.split(" FROM ", 1)[1].split(" ", 1)[0]
        title_field = "Subject" if object_name == "Case" else "Name"
        return {
            "records": [
                {
                    "Id": f"{object_name.lower()}-1",
                    title_field: f"{object_name} Alpha",
                    "LastModifiedDate": "2026-05-01T00:00:00Z",
                    "attributes": {"type": object_name},
                }
            ]
        }


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeNotionClient:
    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "FakeNotionClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        path = urlparse(url).path
        if path == "/v1/users/me":
            return FakeResponse({"object": "user", "id": "bot"})
        if path == "/v1/pages/page-1":
            return FakeResponse(
                {
                    "object": "page",
                    "id": "page-1",
                    "properties": {"Name": {"title": [{"plain_text": "Runbook"}]}},
                }
            )
        if path == "/v1/blocks/page-1/children":
            return FakeResponse(
                {
                    "results": [
                        {
                            "object": "block",
                            "id": "block-1",
                            "type": "paragraph",
                            "paragraph": {"rich_text": [{"plain_text": "Deploy steps"}]},
                        }
                    ]
                }
            )
        if path == "/v1/databases/db-1":
            return FakeResponse({"object": "database", "id": "db-1", "title": []})
        return FakeResponse({}, status_code=404)

    async def post(self, url: str, **_kwargs) -> FakeResponse:
        path = urlparse(url).path
        if path == "/v1/search":
            return FakeResponse(
                {
                    "results": [
                        {
                            "object": "page",
                            "id": "page-1",
                            "last_edited_time": "2026-05-01T00:00:00Z",
                            "properties": {"Name": {"title": [{"plain_text": "Runbook"}]}},
                        }
                    ]
                }
            )
        return FakeResponse({}, status_code=404)


class FakeSaaSProbeFailureClient:
    status_code = 401

    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "FakeSaaSProbeFailureClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse({}, status_code=self.status_code)


@pytest.mark.asyncio
async def test_salesforce_introspect_list_fetch_and_query(monkeypatch) -> None:
    adapter = get_adapter("salesforce", {"connector_key": "salesforce"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeSalesforce())
    config = {
        "username": "svc",
        "password": "secret",
        "security_token": "token",
        "objects": "Account,Case",
    }

    probe = await adapter.probe(config)
    introspection = await adapter.introspect(config)
    assets = await adapter.list_assets(config)
    query = await adapter.execute_read(
        {
            "operation": "query",
            "connection_config": config,
            "soql": "SELECT Id, Name FROM Account LIMIT 1",
        }
    )
    account = json.loads(
        (await adapter.fetch_asset(config, "salesforce://Account/account-1")).decode()
    )

    assert probe.healthy is True
    assert [obj["object_type"] for obj in introspection["objects"]] == ["Account", "Case"]
    assert "salesforce://Account/account-1" in {asset["asset_path"] for asset in assets}
    assert query["records"][0]["Id"] == "account-1"
    assert account["Name"] == "Account account-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
async def test_salesforce_rest_probe_401_403_429_500_are_unhealthy(
    monkeypatch, status_code: int
) -> None:
    FakeSaaSProbeFailureClient.status_code = status_code
    monkeypatch.setattr("httpx.AsyncClient", FakeSaaSProbeFailureClient)
    adapter = get_adapter("salesforce", {"connector_key": "salesforce"})

    probe = await adapter.probe(
        {
            "base_url": "https://salesforce.example.test",
            "token": "x",
            "allow_private_egress": True,
        }
    )

    assert probe.healthy is False
    assert probe.error == f"HTTP {status_code}"


@pytest.mark.asyncio
async def test_notion_probe_search_fetch_and_sync(monkeypatch) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakeNotionClient)
    adapter = get_adapter("notion", {"connector_key": "notion"})
    config = {"token": "secret", "workspace": "acme", "allow_private_egress": True}

    probe = await adapter.probe(config)
    assets = await adapter.list_assets(config)
    page = json.loads((await adapter.fetch_asset(config, "notion://page/page-1")).decode())
    sync = await adapter.sync_incremental(config, {})

    assert probe.healthy is True
    assert assets[0]["asset_path"] == "notion://page/page-1"
    assert assets[0]["title"] == "Runbook"
    assert page["page"]["id"] == "page-1"
    assert page["blocks"][0]["paragraph"]["rich_text"][0]["plain_text"] == "Deploy steps"
    assert sync["assets"][0]["asset_path"] == "notion://page/page-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
async def test_notion_probe_401_403_429_500_are_unhealthy(monkeypatch, status_code: int) -> None:
    FakeSaaSProbeFailureClient.status_code = status_code
    monkeypatch.setattr("httpx.AsyncClient", FakeSaaSProbeFailureClient)
    adapter = get_adapter("notion", {"connector_key": "notion"})

    probe = await adapter.probe({"token": "secret", "allow_private_egress": True})

    assert probe.healthy is False
    assert probe.error == f"HTTP {status_code}"
