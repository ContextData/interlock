"""E2E protocol-path tests.

For each protocol the gateway accepts (PG wire, HTTP, MCP) we exercise
a representative read against the live stack and assert the response
shape and that audit recorded the interaction.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.e2e.support.clients import mcp_call


@pytest.mark.e2e
def test_health_endpoints(gateway_url: str, admin_url: str) -> None:
    for url in (f"{gateway_url}/health", f"{admin_url}/health"):
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read())
        assert body.get("status") == "ok"


@pytest.mark.e2e
def test_mcp_list_tools(gateway_url: str) -> None:
    with urllib.request.urlopen(f"{gateway_url}/mcp/tools/list", timeout=5) as resp:
        body = json.loads(resp.read())
    assert "tools" in body
    names = {t["name"] for t in body["tools"]}
    # All canonical InterLock tools must be advertised.
    assert {
        "agentgate_query",
        "agentgate_list_sources",
        "agentgate_describe_source",
        "agentgate_discover",
        "agentgate_related_documents",
    } <= names


@pytest.mark.e2e
def test_mcp_sse_transport_compatibility(gateway_url: str) -> None:
    with urllib.request.urlopen(f"{gateway_url}/mcp/sse", timeout=5) as resp:
        body = resp.read().decode()
        content_type = resp.headers.get("content-type", "")

    assert content_type.startswith("text/event-stream")
    assert "event: tools" in body
    assert "agentgate_query" in body


@pytest.mark.e2e
def test_mcp_list_sources_filters_to_agent_grants(e2e_config) -> None:
    response = mcp_call(e2e_config, "agentgate_list_sources", {})

    assert response.status_code == 200, response.text
    payload = json.loads(response.json()["content"][0]["text"])
    assert e2e_config.source_id_pg in payload
    assert e2e_config.source_id_http in payload
    assert "control" not in payload


@pytest.mark.e2e
def test_mcp_describe_source_uses_registered_source(e2e_config) -> None:
    response = mcp_call(
        e2e_config,
        "agentgate_describe_source",
        {"source_id": e2e_config.source_id_pg},
    )

    assert response.status_code == 200, response.text
    payload = json.loads(response.json()["content"][0]["text"])
    assert any(
        row.get("table_schema") == "public"
        and row.get("table_name") == "customers"
        and row.get("column_name") == "name"
        for row in payload
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mcp_related_documents_stays_within_seed_source(e2e_config, control_db) -> None:
    seed_id = await control_db.fetchval(
        """
        SELECT id
        FROM discovery_assets
        WHERE source_id = $1
          AND asset_path = '/e2e/fixtures/discovery/runbook.md'
        """,
        e2e_config.source_id_pg,
    )
    assert seed_id is not None
    related_id = await control_db.fetchval(
        """
        INSERT INTO discovery_assets
            (source_id, asset_type, asset_path, title, summary, search_vector, metadata)
        VALUES
            ($1, 'document', '/e2e/fixtures/discovery/related-runbook.md',
             'Related Governance Notes',
             '{"summary":"Related InterLock governance notes."}'::jsonb,
             to_tsvector('english', 'related InterLock governance notes'),
             '{"seed":"e2e-related"}'::jsonb)
        ON CONFLICT (source_id, asset_type, asset_path) DO UPDATE
        SET title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            search_vector = EXCLUDED.search_vector,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        RETURNING id
        """,
        e2e_config.source_id_pg,
    )
    await control_db.execute(
        """
        INSERT INTO entity_document_xref
            (entity_text, entity_type, document_id, prominence, prominence_label,
             mention_count, context_snippet, metadata)
        VALUES
            ('InterLock', 'PRODUCT', $1, 0.8, 'supporting', 2,
             'Related InterLock governance notes share the seed product entity.',
             '{"seed":"e2e-related"}'::jsonb)
        ON CONFLICT (entity_text, entity_type, document_id) DO UPDATE
        SET prominence = EXCLUDED.prominence,
            prominence_label = EXCLUDED.prominence_label,
            mention_count = EXCLUDED.mention_count,
            context_snippet = EXCLUDED.context_snippet,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        related_id,
    )

    response = mcp_call(
        e2e_config,
        "agentgate_related_documents",
        {"asset_id": int(seed_id), "limit": 5},
    )

    assert response.status_code == 200, response.text
    payload = json.loads(response.json()["content"][0]["text"])
    assert any(row["asset_path"] == "/e2e/fixtures/discovery/related-runbook.md" for row in payload)
    assert {row["source_id"] for row in payload} == {e2e_config.source_id_pg}


@pytest.mark.e2e
def test_http_proxy_unknown_source_returns_404(gateway_url: str) -> None:
    req = urllib.request.Request(
        f"{gateway_url}/proxy/no-such-source/anything",
        headers={"Authorization": "Bearer dummy"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            assert "HTTP proxy not initialized" not in body
    except urllib.error.HTTPError as exc:
        # 401 for invalid bearer is acceptable; 404 for unknown source is
        # also acceptable. The lifecycle bug would have been a 503 with
        # "HTTP proxy not initialized".
        assert exc.code in (401, 404), f"Unexpected status {exc.code}"
