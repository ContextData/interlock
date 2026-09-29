from __future__ import annotations

import pytest

from interlock.connections.connectors import GwsRunner, get_adapter
from interlock.connections.source_config import SourceConfigValidationError, validate_source_config
from interlock.security.egress import EgressBlockedError


class ExplodingHTTPClient:
    created = False

    def __init__(self, *_, **__) -> None:
        self.__class__.created = True
        raise AssertionError("HTTP client should not be created for blocked egress")


def test_source_config_rejects_runtime_executable_key() -> None:
    with pytest.raises(SourceConfigValidationError, match="gws_binary"):
        validate_source_config(
            {"gws_binary": "/tmp/evil-gws"},
            connector_key="google_workspace",
            allowed_fields=("workspace_domain",),
        )


def test_source_config_blocks_private_http_endpoint_by_default() -> None:
    with pytest.raises(EgressBlockedError):
        validate_source_config(
            {"base_url": "http://169.254.169.254/latest/meta-data"},
            connector_key="opensearch",
            allowed_fields=("base_url",),
        )


def test_source_config_allows_private_endpoint_only_with_explicit_override() -> None:
    validate_source_config(
        {
            "base_url": "http://169.254.169.254/latest/meta-data",
            "allow_private_egress": True,
        },
        connector_key="opensearch",
        allowed_fields=("base_url",),
    )


@pytest.mark.asyncio
async def test_opensearch_probe_blocks_private_endpoint_before_http_client(monkeypatch) -> None:
    ExplodingHTTPClient.created = False
    monkeypatch.setattr("httpx.AsyncClient", ExplodingHTTPClient)
    adapter = get_adapter("opensearch", {"connector_key": "opensearch"})

    result = await adapter.probe({"base_url": "http://2130706433:9200"})

    assert result.healthy is False
    assert "private" in (result.error or "").lower()
    assert ExplodingHTTPClient.created is False


@pytest.mark.asyncio
async def test_qdrant_runtime_blocks_private_endpoint_before_http_client(monkeypatch) -> None:
    ExplodingHTTPClient.created = False
    monkeypatch.setattr("httpx.AsyncClient", ExplodingHTTPClient)
    adapter = get_adapter("qdrant", {"connector_key": "qdrant"})

    with pytest.raises(EgressBlockedError):
        await adapter.execute_read(
            {
                "operation": "query",
                "connection_config": {"base_url": "http://0x7f000001:6333"},
                "collection": "claims",
            }
        )

    assert ExplodingHTTPClient.created is False


@pytest.mark.asyncio
async def test_s3_runtime_blocks_private_endpoint_before_s3_client() -> None:
    adapter = get_adapter("s3", {"connector_key": "s3"})

    with pytest.raises(EgressBlockedError):
        await adapter.list_assets(
            {
                "bucket": "docs",
                "endpoint_url": "http://0177.0.0.1:9000",
            }
        )


@pytest.mark.asyncio
async def test_mysql_probe_blocks_private_host_before_driver_import() -> None:
    adapter = get_adapter("mysql", {"connector_key": "mysql"})

    result = await adapter.probe({"host": "127.0.0.1", "port": 3306})

    assert result.healthy is False
    assert "private" in (result.error or "").lower()
    assert "aiomysql" not in (result.error or "").lower()


@pytest.mark.asyncio
async def test_gws_runner_rejects_configured_binary_path() -> None:
    runner = GwsRunner()

    with pytest.raises(PermissionError, match="gws_binary"):
        await runner.run_json(
            {"gws_binary": "/tmp/evil-gws"},
            ("drive", "about", "get"),
        )
