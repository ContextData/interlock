"""Compose-backed certification slice for new enterprise connectors."""

from __future__ import annotations

import json

import pytest

from tests.e2e.support.clients import mcp_call, wait_for


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_attr", "connector_key", "expected_asset"),
    [
        ("source_id_spaces", "digitalocean_spaces", "s3://interlock-spaces-e2e/org/runbook.md"),
        ("source_id_opensearch", "opensearch", "opensearch://claims-2026/doc-1"),
        ("source_id_qdrant", "qdrant", "qdrant://claims/point/42"),
        ("source_id_salesforce", "salesforce", "salesforce://Account/001-e2e"),
        ("source_id_notion", "notion", "notion://page/page-e2e"),
    ],
)
async def test_seeded_enterprise_connector_probe_sync_and_index(
    e2e_config,
    control_db,
    admin_session,
    source_attr: str,
    connector_key: str,
    expected_asset: str,
) -> None:
    source_id = getattr(e2e_config, source_attr)
    source = await control_db.fetchrow(
        "SELECT source_type, metadata FROM data_sources WHERE source_id = $1",
        source_id,
    )
    assert source is not None
    metadata = source["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["connector_key"] == connector_key

    probe = admin_session.client.post(
        f"/api/data-sources/{source_id}/test",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert probe.status_code == 200
    assert probe.json()["ok"] is True

    safe_source = admin_session.client.get(f"/api/data-sources/{source_id}")
    assert safe_source.status_code == 200
    safe_text = json.dumps(safe_source.json())
    assert "e2e-token" not in safe_text
    assert "e2e-s3-secret-key" not in safe_text

    sync = admin_session.client.post(
        f"/api/ingestion/sources/{source_id}/sync",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert sync.status_code == 200, sync.text
    payload = sync.json()
    assert payload["connector_key"] == connector_key
    assert payload["assets_seen"] >= 1

    job_count = await control_db.fetchval(
        """
        SELECT COUNT(*)
        FROM ingestion_jobs
        WHERE source_id = $1
          AND file_path = $2
        """,
        source_id,
        expected_asset,
    )
    assert job_count >= 1

    async def completed_asset():
        return await control_db.fetchrow(
            """
            SELECT asset_path, search_vector IS NOT NULL AS indexed
            FROM discovery_assets
            WHERE source_id = $1
              AND asset_path = $2
            """,
            source_id,
            expected_asset,
        )

    asset = await wait_for(completed_asset, timeout_seconds=45, interval_seconds=1)
    assert asset is not None
    assert asset["indexed"] is True


@pytest.mark.e2e
def test_enterprise_connector_discovery_results_are_searchable(e2e_config) -> None:
    checks = [
        (
            e2e_config.source_id_opensearch,
            "OpenSearch indexed document",
            "opensearch://claims-2026/doc-1",
        ),
        (e2e_config.source_id_qdrant, "Qdrant point", "qdrant://claims/point/42"),
        (e2e_config.source_id_salesforce, "Acme Claims", "salesforce://Account/001-e2e"),
        (e2e_config.source_id_notion, "Claims operations runbook", "notion://page/page-e2e"),
    ]
    for source_id, query, expected_asset in checks:
        response = mcp_call(
            e2e_config,
            "agentgate_discover",
            {"source_id": source_id, "query": query, "limit": 5},
        )
        assert response.status_code == 200, response.text
        results = json.loads(response.json()["content"][0]["text"])
        assert any(result["asset_path"] == expected_asset for result in results)
