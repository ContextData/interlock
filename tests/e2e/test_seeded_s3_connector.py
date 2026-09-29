"""S3 connector E2E certification tests, against the compose S3 upstream."""

from __future__ import annotations

import json

import pytest

from interlock.connections.connectors import get_adapter
from tests.e2e.support.clients import mcp_call, wait_for


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_seeded_s3_source_roles_and_sync_create_ingestion_jobs(
    e2e_config,
    control_db,
    admin_session,
) -> None:
    source = await control_db.fetchrow(
        "SELECT source_type, metadata FROM data_sources WHERE source_id = $1",
        e2e_config.source_id_s3,
    )
    assert source is not None
    assert source["source_type"] == "s3"
    metadata = source["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["connector_key"] == "s3"

    probe = admin_session.client.post(
        f"/api/data-sources/{e2e_config.source_id_s3}/test",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert probe.status_code == 200
    assert probe.json()["ok"] is True

    safe_source = admin_session.client.get(f"/api/data-sources/{e2e_config.source_id_s3}")
    assert safe_source.status_code == 200
    safe_payload = safe_source.json()
    safe_text = json.dumps(safe_payload)
    assert e2e_config.s3_access_key not in safe_text
    assert e2e_config.s3_secret_key not in safe_text
    assert safe_payload["connection_config"]["aws_access_key_id"] == "<configured>"
    assert safe_payload["connection_config"]["aws_secret_access_key"] == "<configured>"

    role_count = await control_db.fetchval(
        "SELECT COUNT(*) FROM source_roles WHERE source_id = $1 AND enabled = TRUE",
        e2e_config.source_id_s3,
    )
    assert role_count >= 5

    response = admin_session.client.post(
        f"/api/ingestion/sources/{e2e_config.source_id_s3}/sync",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["connector_key"] == "s3"
    assert payload["assets_seen"] >= 1

    job_count = await control_db.fetchval(
        """
        SELECT COUNT(*)
        FROM ingestion_jobs
        WHERE source_id = $1
          AND file_path LIKE 's3://interlock-e2e/%'
        """,
        e2e_config.source_id_s3,
    )
    assert job_count >= 1
    job_metadata = await control_db.fetchval(
        """
        SELECT metadata
        FROM ingestion_jobs
        WHERE source_id = $1
          AND file_path = 's3://interlock-e2e/discovery/runbook.md'
        """,
        e2e_config.source_id_s3,
    )
    if isinstance(job_metadata, str):
        job_metadata = json.loads(job_metadata)
    assert job_metadata["bucket"] == e2e_config.s3_bucket
    assert job_metadata["file_extension"] == ".md"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_seeded_s3_ingestion_job_completes_and_indexes_document(
    e2e_config,
    control_db,
    admin_session,
) -> None:
    response = admin_session.client.post(
        f"/api/ingestion/sources/{e2e_config.source_id_s3}/sync",
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert response.status_code == 200

    async def completed_job():
        return await control_db.fetchrow(
            """
            SELECT id, status, stage, error_message
            FROM ingestion_jobs
            WHERE source_id = $1
              AND file_path = 's3://interlock-e2e/discovery/runbook.md'
              AND status = 'completed'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            e2e_config.source_id_s3,
        )

    job = await wait_for(completed_job, timeout_seconds=30, interval_seconds=1)
    assert job is not None, "S3 ingestion job did not complete"
    assert job["error_message"] in (None, "")

    asset = await control_db.fetchrow(
        """
        SELECT title, asset_type, asset_path, search_vector IS NOT NULL AS indexed
        FROM discovery_assets
        WHERE source_id = $1
          AND asset_path = 's3://interlock-e2e/discovery/runbook.md'
        """,
        e2e_config.source_id_s3,
    )
    assert asset is not None
    assert asset["asset_type"] == "file"
    assert asset["indexed"] is True

    discovery = mcp_call(
        e2e_config,
        "agentgate_discover",
        {
            "source_id": e2e_config.source_id_s3,
            "query": "InterLock governance runbook",
            "limit": 5,
        },
    )
    assert discovery.status_code == 200, discovery.text
    results = json.loads(discovery.json()["content"][0]["text"])
    assert any(
        result["asset_path"] == "s3://interlock-e2e/discovery/runbook.md" for result in results
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_seeded_s3_write_and_delete_against_the_upstream(e2e_config) -> None:
    adapter = get_adapter("s3", {"connector_key": "s3"})
    connection_config = {
        "bucket": e2e_config.s3_bucket,
        "region_name": "us-east-1",
        "endpoint_url": e2e_config.s3_url,
        "allow_private_egress": True,
        "aws_access_key_id": e2e_config.s3_access_key,
        "aws_secret_access_key": e2e_config.s3_secret_key,
    }
    asset_ref = f"s3://{e2e_config.s3_bucket}/certification/write-delete.txt"

    put_result = await adapter.execute_write(
        {
            "operation": "write",
            "connection_config": connection_config,
            "asset_ref": asset_ref,
            "body": "InterLock local S3 write/delete certification",
        }
    )
    fetched = await adapter.execute_read(
        {
            "operation": "read",
            "connection_config": connection_config,
            "asset_ref": asset_ref,
        }
    )
    delete_result = await adapter.execute_write(
        {
            "operation": "delete",
            "connection_config": connection_config,
            "asset_ref": asset_ref,
        }
    )
    remaining_assets = await adapter.execute_read(
        {
            "operation": "list",
            "connection_config": {
                **connection_config,
                "prefix": "certification/",
            },
        }
    )

    assert put_result["asset_ref"] == asset_ref
    assert fetched == b"InterLock local S3 write/delete certification"
    assert delete_result["asset_ref"] == asset_ref
    assert delete_result["deleted"] is True
    assert asset_ref not in {asset["asset_path"] for asset in remaining_assets["assets"]}
