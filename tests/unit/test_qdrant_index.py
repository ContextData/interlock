"""Tests for the QdrantIndex semantic vector index.

All tests mock httpx calls - no running Qdrant instance required.
Validates graceful degradation, search, upsert, delete, count, filter
conversion, and TTL handling.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interlock.cache.qdrant_index import QdrantIndex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(status_code: int = 200, json_data: dict | None = None):
    """Create a mock httpx Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = ""
    resp.json.return_value = json_data or {}
    return resp


def _point_id_for(key: str) -> str:
    """Compute the deterministic UUID5 that QdrantIndex uses for a key."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_qdrant_index_satisfies_semantic_index_protocol():
    """QdrantIndex should satisfy the SemanticIndex protocol."""
    from interlock.cache.faiss_index import SemanticIndex

    assert isinstance(QdrantIndex(), SemanticIndex)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_when_httpx_not_installed():
    """When httpx is not available, _available should be False."""
    idx = QdrantIndex()
    with patch("interlock.cache.qdrant_index._httpx", None):
        await idx.initialize()
    assert idx.available is False


@pytest.mark.asyncio
async def test_initialize_when_qdrant_unreachable():
    """When Qdrant server is unreachable, _available should be False."""
    mock_httpx = MagicMock()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=ConnectionError("refused"))
    mock_httpx.AsyncClient.return_value = mock_client

    idx = QdrantIndex()
    with patch("interlock.cache.qdrant_index._httpx", mock_httpx):
        await idx.initialize()

    assert idx.available is False


@pytest.mark.asyncio
async def test_initialize_creates_collection_on_404():
    """When collection doesn't exist (404), initialize should create it."""
    mock_httpx = MagicMock()
    mock_client = AsyncMock()

    # First call: GET collection returns 404
    # Second call: PUT to create collection returns 200
    mock_client.get = AsyncMock(return_value=_make_mock_response(404))
    mock_client.put = AsyncMock(return_value=_make_mock_response(200))

    mock_httpx.AsyncClient.return_value = mock_client

    idx = QdrantIndex(collection_name="test_col", dimension=128)
    with patch("interlock.cache.qdrant_index._httpx", mock_httpx):
        await idx.initialize()

    assert idx.available is True
    # Verify PUT was called to create collection
    mock_client.put.assert_called_once()
    call_args = mock_client.put.call_args
    assert "/collections/test_col" in call_args[0][0]


@pytest.mark.asyncio
async def test_initialize_existing_collection():
    """When collection already exists (200), initialize succeeds without creating."""
    mock_httpx = MagicMock()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_make_mock_response(200))
    mock_httpx.AsyncClient.return_value = mock_client

    idx = QdrantIndex()
    with patch("interlock.cache.qdrant_index._httpx", mock_httpx):
        await idx.initialize()

    assert idx.available is True
    mock_client.put.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_with_api_key():
    """API key should be included in client headers."""
    mock_httpx = MagicMock()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_make_mock_response(200))
    mock_httpx.AsyncClient.return_value = mock_client

    idx = QdrantIndex(api_key="test-secret")
    with patch("interlock.cache.qdrant_index._httpx", mock_httpx):
        await idx.initialize()

    # Check headers passed to AsyncClient
    call_kwargs = mock_httpx.AsyncClient.call_args[1]
    assert call_kwargs["headers"]["api-key"] == "test-secret"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_when_not_available():
    """search() returns empty list when not initialized."""
    idx = QdrantIndex()
    result = await idx.search([0.0] * 384)
    assert result == []


@pytest.mark.asyncio
async def test_search_returns_semantic_matches():
    """search() should parse Qdrant response into SemanticMatch objects."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=_make_mock_response(
            200,
            {
                "result": [
                    {
                        "id": "some-uuid",
                        "score": 0.95,
                        "payload": {
                            "cache_key": "query-1",
                            "meta": {"source": "pg", "table": "users"},
                        },
                    },
                    {
                        "id": "other-uuid",
                        "score": 0.87,
                        "payload": {
                            "cache_key": "query-2",
                            "meta": {"source": "pg"},
                        },
                    },
                ]
            },
        )
    )
    idx._client = mock_client

    results = await idx.search([0.1] * 384, top_k=5)

    assert len(results) == 2
    assert results[0].key == "query-1"
    assert results[0].score == 0.95
    assert results[0].metadata == {"source": "pg", "table": "users"}
    assert results[1].key == "query-2"


@pytest.mark.asyncio
async def test_search_with_filters():
    """search() with filters should include metadata filter conditions."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_mock_response(200, {"result": []}))
    idx._client = mock_client

    await idx.search([0.1] * 384, top_k=3, filters={"source": "pg"})

    # Verify the filter was included in the request
    call_args = mock_client.post.call_args
    body = call_args[1]["json"]
    assert "filter" in body
    must_conditions = body["filter"]["must"]
    # Should have TTL filter + metadata filter
    assert len(must_conditions) == 2
    # Second condition should be the metadata filter
    meta_filter = must_conditions[1]
    assert meta_filter["key"] == "meta.source"
    assert meta_filter["match"]["value"] == "pg"


@pytest.mark.asyncio
async def test_search_error_returns_empty():
    """search() should return empty list on Qdrant error."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_mock_response(500, {}))
    idx._client = mock_client

    results = await idx.search([0.1] * 384)
    assert results == []


@pytest.mark.asyncio
async def test_search_exception_returns_empty():
    """search() should return empty list on network exception."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=ConnectionError("timeout"))
    idx._client = mock_client

    results = await idx.search([0.1] * 384)
    assert results == []


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_constructs_correct_request():
    """upsert() should PUT a point with the correct structure."""
    idx = QdrantIndex(collection_name="test")
    idx._available = True

    mock_client = AsyncMock()
    mock_client.put = AsyncMock(return_value=_make_mock_response(200))
    idx._client = mock_client

    embedding = [0.5] * 384
    await idx.upsert("my-key", embedding, {"source": "pg"}, ttl_seconds=120)

    mock_client.put.assert_called_once()
    call_args = mock_client.put.call_args
    assert "/collections/test/points" in call_args[0][0]

    body = call_args[1]["json"]
    point = body["points"][0]
    assert point["id"] == _point_id_for("my-key")
    assert point["vector"] == embedding
    assert point["payload"]["cache_key"] == "my-key"
    assert point["payload"]["meta"] == {"source": "pg"}
    assert point["payload"]["expires_at"] > time.time()


@pytest.mark.asyncio
async def test_upsert_without_ttl_has_no_expires():
    """upsert() without ttl_seconds should not include expires_at."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.put = AsyncMock(return_value=_make_mock_response(200))
    idx._client = mock_client

    await idx.upsert("key1", [0.1] * 384, {})

    body = mock_client.put.call_args[1]["json"]
    payload = body["points"][0]["payload"]
    assert "expires_at" not in payload


@pytest.mark.asyncio
async def test_upsert_when_not_available():
    """upsert() should be a no-op when not available."""
    idx = QdrantIndex()
    # Not initialized, _available is False
    await idx.upsert("key", [0.1] * 384, {})
    # Should not raise


@pytest.mark.asyncio
async def test_upsert_error_logged_not_raised():
    """upsert() should log errors but not raise."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.put = AsyncMock(side_effect=ConnectionError("down"))
    idx._client = mock_client

    # Should not raise
    await idx.upsert("key", [0.1] * 384, {})


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_constructs_correct_request():
    """delete() should POST the correct point ID for deletion."""
    idx = QdrantIndex(collection_name="col")
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_mock_response(200))
    idx._client = mock_client

    await idx.delete("my-key")

    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "/collections/col/points/delete" in call_args[0][0]

    body = call_args[1]["json"]
    assert body["points"] == [_point_id_for("my-key")]


@pytest.mark.asyncio
async def test_delete_when_not_available():
    """delete() should be a no-op when not available."""
    idx = QdrantIndex()
    await idx.delete("key")
    # Should not raise


# ---------------------------------------------------------------------------
# Count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_returns_value():
    """count() should return the count from Qdrant response."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_mock_response(200, {"result": {"count": 42}}))
    idx._client = mock_client

    assert await idx.count() == 42


@pytest.mark.asyncio
async def test_count_when_not_available():
    """count() returns 0 when not available."""
    idx = QdrantIndex()
    assert await idx.count() == 0


@pytest.mark.asyncio
async def test_count_error_returns_zero():
    """count() returns 0 on Qdrant error."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_mock_response(500, {}))
    idx._client = mock_client

    assert await idx.count() == 0


# ---------------------------------------------------------------------------
# Prune expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_expired_when_not_available():
    """prune_expired() returns 0 when not available."""
    idx = QdrantIndex()
    assert await idx.prune_expired() == 0


@pytest.mark.asyncio
async def test_prune_expired_deletes_scrolled_points():
    """prune_expired() should scroll for expired points and delete them."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()

    # Scroll returns 2 expired points
    scroll_response = _make_mock_response(
        200,
        {
            "result": {
                "points": [
                    {"id": "uuid-1"},
                    {"id": "uuid-2"},
                ]
            }
        },
    )
    # Delete returns success
    delete_response = _make_mock_response(200)

    mock_client.post = AsyncMock(side_effect=[scroll_response, delete_response])
    idx._client = mock_client

    removed = await idx.prune_expired()
    assert removed == 2
    assert mock_client.post.call_count == 2

    # Verify delete call
    delete_call = mock_client.post.call_args_list[1]
    assert "delete" in delete_call[0][0]
    assert delete_call[1]["json"]["points"] == ["uuid-1", "uuid-2"]


@pytest.mark.asyncio
async def test_prune_expired_no_expired_points():
    """prune_expired() returns 0 when no points are expired."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_mock_response(200, {"result": {"points": []}}))
    idx._client = mock_client

    removed = await idx.prune_expired()
    assert removed == 0


# ---------------------------------------------------------------------------
# Filter conversion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_conversion_multiple_metadata_keys():
    """Multiple filter keys should produce multiple must conditions."""
    idx = QdrantIndex()
    idx._available = True

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_mock_response(200, {"result": []}))
    idx._client = mock_client

    await idx.search(
        [0.1] * 384,
        top_k=5,
        filters={"source": "pg", "table": "users"},
    )

    body = mock_client.post.call_args[1]["json"]
    must_conditions = body["filter"]["must"]
    # TTL filter + 2 metadata filters
    assert len(must_conditions) == 3

    meta_keys = {c["key"] for c in must_conditions if "key" in c}
    assert "meta.source" in meta_keys
    assert "meta.table" in meta_keys


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_closes_client():
    """shutdown() should close the httpx client and set unavailable."""
    idx = QdrantIndex()
    idx._available = True
    mock_client = AsyncMock()
    idx._client = mock_client

    await idx.shutdown()

    mock_client.aclose.assert_called_once()
    assert idx.available is False
    assert idx._client is None
