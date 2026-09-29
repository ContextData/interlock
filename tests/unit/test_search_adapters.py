from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from interlock.connections.connectors import get_adapter


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSearchClient:
    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "FakeSearchClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        path = urlparse(url).path
        if path == "/_cluster/health":
            return FakeResponse({"status": "green"})
        if path == "/_cat/indices":
            return FakeResponse(
                [
                    {"index": "claims-2026", "docs.count": "1"},
                    {"index": "runbooks", "docs.count": "1"},
                ]
            )
        if path == "/claims-2026/_doc/doc-1":
            return FakeResponse(
                {"_index": "claims-2026", "_id": "doc-1", "_source": {"title": "Claim 1"}}
            )
        return FakeResponse({}, status_code=404)

    async def post(self, url: str, **_kwargs) -> FakeResponse:
        path = urlparse(url).path
        if path.endswith("/_search"):
            index = path.strip("/").split("/")[0]
            return FakeResponse(
                {
                    "hits": {
                        "hits": [
                            {
                                "_index": index,
                                "_id": "doc-1",
                                "_source": {
                                    "title": "Claim 1",
                                    "updated_at": "2026-05-01T00:00:00Z",
                                },
                            }
                        ]
                    }
                }
            )
        return FakeResponse({}, status_code=404)


class FakeQdrantClient:
    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "FakeQdrantClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        path = urlparse(url).path
        if path == "/collections":
            return FakeResponse({"result": {"collections": [{"name": "claims"}]}})
        if path == "/collections/claims":
            return FakeResponse({"result": {"status": "green", "points_count": 1}})
        return FakeResponse({}, status_code=404)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        path = urlparse(url).path
        if path == "/collections/claims/points/scroll":
            return FakeResponse(
                {
                    "result": {
                        "points": [
                            {
                                "id": "42",
                                "payload": {"title": "Claim vector", "category": "claims"},
                            }
                        ]
                    }
                }
            )
        if path == "/collections/claims/points":
            ids = kwargs.get("json", {}).get("ids", [])
            return FakeResponse({"result": [{"id": ids[0], "payload": {"title": "Claim vector"}}]})
        return FakeResponse({}, status_code=404)


class FakeSearchProbeFailureClient:
    status_code = 401

    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "FakeSearchProbeFailureClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse({}, status_code=self.status_code)


class FakeQdrantProbeFailureClient(FakeSearchProbeFailureClient):
    pass


@pytest.mark.asyncio
async def test_opensearch_probe_list_query_and_fetch(monkeypatch) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakeSearchClient)
    adapter = get_adapter("opensearch", {"connector_key": "opensearch"})
    config = {"base_url": "http://opensearch:9200", "allow_private_egress": True}

    probe = await adapter.probe(config)
    assets = await adapter.list_assets(config)
    query = await adapter.execute_read(
        {
            "operation": "query",
            "connection_config": config,
            "index": "claims-2026",
            "query": {"query": {"match_all": {}}},
        }
    )
    document = json.loads(
        (await adapter.fetch_asset(config, "opensearch://claims-2026/doc-1")).decode()
    )

    assert probe.healthy is True
    assert "opensearch://claims-2026" in {asset["asset_path"] for asset in assets}
    assert "opensearch://claims-2026/doc-1" in {asset["asset_path"] for asset in assets}
    assert query["hits"]["hits"][0]["_id"] == "doc-1"
    assert document["_source"]["title"] == "Claim 1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
async def test_opensearch_probe_401_403_429_500_are_unhealthy(
    monkeypatch, status_code: int
) -> None:
    FakeSearchProbeFailureClient.status_code = status_code
    monkeypatch.setattr("httpx.AsyncClient", FakeSearchProbeFailureClient)
    adapter = get_adapter("opensearch", {"connector_key": "opensearch"})

    probe = await adapter.probe(
        {"base_url": "http://opensearch:9200", "allow_private_egress": True}
    )

    assert probe.healthy is False
    assert probe.error == f"HTTP {status_code}"


@pytest.mark.asyncio
async def test_elasticsearch_uses_same_search_adapter(monkeypatch) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakeSearchClient)
    adapter = get_adapter("elasticsearch", {"connector_key": "elasticsearch"})

    assets = await adapter.list_assets(
        {
            "base_url": "http://elasticsearch:9200",
            "indices": "claims-2026",
            "allow_private_egress": True,
        }
    )

    assert adapter.definition.key == "elasticsearch"
    assert assets[0]["asset_path"] == "elasticsearch://claims-2026"


@pytest.mark.asyncio
async def test_qdrant_probe_list_query_and_fetch(monkeypatch) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakeQdrantClient)
    adapter = get_adapter("qdrant", {"connector_key": "qdrant"})
    config = {"base_url": "http://qdrant:6333", "allow_private_egress": True}

    probe = await adapter.probe(config)
    introspection = await adapter.introspect(config)
    assets = await adapter.list_assets(config)
    query = await adapter.execute_read(
        {
            "operation": "query",
            "connection_config": config,
            "collection": "claims",
            "limit": 1,
        }
    )
    point = json.loads((await adapter.fetch_asset(config, "qdrant://claims/point/42")).decode())

    assert probe.healthy is True
    assert introspection["collections"][0]["collection"] == "claims"
    assert "qdrant://claims/point/42" in {asset["asset_path"] for asset in assets}
    assert query["result"]["points"][0]["id"] == "42"
    assert point["result"][0]["payload"]["title"] == "Claim vector"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
async def test_qdrant_probe_401_403_429_500_are_unhealthy(monkeypatch, status_code: int) -> None:
    FakeQdrantProbeFailureClient.status_code = status_code
    monkeypatch.setattr("httpx.AsyncClient", FakeQdrantProbeFailureClient)
    adapter = get_adapter("qdrant", {"connector_key": "qdrant"})

    probe = await adapter.probe({"base_url": "http://qdrant:6333", "allow_private_egress": True})

    assert probe.healthy is False
    assert probe.error == f"HTTP {status_code}"
