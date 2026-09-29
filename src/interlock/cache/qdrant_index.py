"""Qdrant-backed semantic index using the Qdrant REST API via httpx.

Implements the SemanticIndex protocol as an alternative to FAISSIndex.
Unlike FAISSIndex which is in-process, Qdrant is an external service
that supports distributed vector search, filtering, and persistence
natively.

All Qdrant communication uses httpx.AsyncClient against the REST API,
keeping dependencies light (no qdrant-client SDK required).

Gracefully degrades if Qdrant is unreachable or httpx is not installed.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from interlock.models import SemanticMatch

logger = logging.getLogger(__name__)

# Optional httpx import
_httpx: Any = None
try:
    import httpx as _httpx  # type: ignore[no-redefine]
except ImportError:
    pass


class QdrantIndex:
    """Qdrant-backed semantic index implementing SemanticIndex protocol.

    Unlike FAISSIndex which is in-process, Qdrant is an external service
    that supports distributed vector search, filtering, and persistence
    natively.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        collection_name: str = "onyx_cache",
        dimension: int = 384,
        api_key: str | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._collection_name = collection_name
        self._dimension = dimension
        self._api_key = api_key
        self._available = False
        self._client: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Connect to Qdrant and ensure collection exists.

        Gracefully degrades if httpx is not installed or the server is
        unreachable.
        """
        if _httpx is None:
            logger.warning(
                "httpx is not installed - QdrantIndex will be unavailable. "
                "Install with: pip install httpx"
            )
            self._available = False
            return

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key

        self._client = _httpx.AsyncClient(
            base_url=self._url,
            headers=headers,
            timeout=10.0,
        )

        # Check connectivity and ensure collection exists
        try:
            resp = await self._client.get(f"/collections/{self._collection_name}")
            if resp.status_code == 404:
                # Collection does not exist - create it
                await self._create_collection()
            elif resp.status_code >= 400:
                logger.error(
                    "Qdrant returned status %d when checking collection %s",
                    resp.status_code,
                    self._collection_name,
                )
                self._available = False
                return

            self._available = True
            logger.info(
                "QdrantIndex initialized (url=%s, collection=%s, dim=%d)",
                self._url,
                self._collection_name,
                self._dimension,
            )
        except Exception:
            logger.exception(
                "Failed to connect to Qdrant at %s - index unavailable",
                self._url,
            )
            self._available = False

    async def _create_collection(self) -> None:
        """Create the Qdrant collection with cosine distance."""
        if self._client is None:
            return

        body = {
            "vectors": {
                "size": self._dimension,
                "distance": "Cosine",
            }
        }
        resp = await self._client.put(
            f"/collections/{self._collection_name}",
            json=body,
        )
        if resp.status_code >= 400:
            logger.error(
                "Failed to create Qdrant collection %s: %d %s",
                self._collection_name,
                resp.status_code,
                resp.text,
            )
            raise RuntimeError(f"Failed to create collection: {resp.status_code}")
        logger.info("Created Qdrant collection %s", self._collection_name)

    async def shutdown(self) -> None:
        """Close the httpx client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._available = False

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        embedding: list[float],
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[SemanticMatch]:
        """Search for similar vectors with optional metadata filtering.

        Expired entries (past their expires_at timestamp) are excluded
        from results via a Qdrant payload filter.
        """
        if not self._available or self._client is None:
            return []

        try:
            body: dict[str, Any] = {
                "vector": embedding,
                "limit": top_k,
                "with_payload": True,
            }

            # Build filter conditions
            must_conditions: list[dict[str, Any]] = []

            # Exclude expired entries
            now = time.time()
            must_conditions.append(
                {
                    "key": "expires_at",
                    "match": {"value": 0},  # placeholder - replaced below
                }
            )
            # Qdrant filter: either no expires_at or expires_at > now
            # Use a "should" with two conditions for TTL filtering
            ttl_filter: dict[str, Any] = {
                "should": [
                    {
                        "is_empty": {
                            "key": "expires_at",
                        }
                    },
                    {
                        "key": "expires_at",
                        "range": {
                            "gt": now,
                        },
                    },
                ]
            }
            # Remove the placeholder and use the ttl filter as a must condition
            must_conditions = [ttl_filter]

            # Add metadata filters (exact match)
            if filters:
                for key, value in filters.items():
                    must_conditions.append(
                        {
                            "key": f"meta.{key}",
                            "match": {"value": value},
                        }
                    )

            if must_conditions:
                body["filter"] = {"must": must_conditions}

            resp = await self._client.post(
                f"/collections/{self._collection_name}/points/search",
                json=body,
            )

            if resp.status_code >= 400:
                logger.error(
                    "Qdrant search failed: %d %s",
                    resp.status_code,
                    resp.text,
                )
                return []

            data = resp.json()
            results: list[SemanticMatch] = []
            for hit in data.get("result", []):
                payload = hit.get("payload", {})
                meta = payload.get("meta", {})
                cache_key = payload.get("cache_key", "")
                score = hit.get("score", 0.0)
                results.append(SemanticMatch(key=cache_key, score=score, metadata=meta))

            return results

        except Exception:
            logger.exception("Qdrant search error")
            return []

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    async def upsert(
        self,
        key: str,
        embedding: list[float],
        metadata: dict,
        ttl_seconds: int | None = None,
    ) -> None:
        """Upsert a vector with metadata into Qdrant.

        TTL is implemented by storing an expires_at timestamp in the
        payload. The search method filters out expired entries.
        """
        if not self._available or self._client is None:
            return

        try:
            expires_at: float | None = None
            if ttl_seconds is not None:
                expires_at = time.time() + ttl_seconds

            # Use a deterministic UUID derived from the key for the point ID
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))

            payload: dict[str, Any] = {
                "cache_key": key,
                "meta": metadata,
            }
            if expires_at is not None:
                payload["expires_at"] = expires_at

            body = {
                "points": [
                    {
                        "id": point_id,
                        "vector": embedding,
                        "payload": payload,
                    }
                ]
            }

            resp = await self._client.put(
                f"/collections/{self._collection_name}/points",
                json=body,
            )

            if resp.status_code >= 400:
                logger.error(
                    "Qdrant upsert failed for key %s: %d %s",
                    key,
                    resp.status_code,
                    resp.text,
                )

        except Exception:
            logger.exception("Qdrant upsert error for key %s", key)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, key: str) -> None:
        """Delete a point by its cache key."""
        if not self._available or self._client is None:
            return

        try:
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))

            body = {"points": [point_id]}

            resp = await self._client.post(
                f"/collections/{self._collection_name}/points/delete",
                json=body,
            )

            if resp.status_code >= 400:
                logger.error(
                    "Qdrant delete failed for key %s: %d %s",
                    key,
                    resp.status_code,
                    resp.text,
                )

        except Exception:
            logger.exception("Qdrant delete error for key %s", key)

    # ------------------------------------------------------------------
    # Prune expired
    # ------------------------------------------------------------------

    async def prune_expired(self) -> int:
        """Remove entries whose TTL has expired.

        Scrolls for points with expires_at < now, then deletes them in
        a batch.
        """
        if not self._available or self._client is None:
            return 0

        try:
            now = time.time()

            # Scroll for expired points
            scroll_body: dict[str, Any] = {
                "filter": {
                    "must": [
                        {
                            "key": "expires_at",
                            "range": {
                                "lt": now,
                                "gt": 0,
                            },
                        }
                    ]
                },
                "limit": 1000,
                "with_payload": False,
                "with_vector": False,
            }

            resp = await self._client.post(
                f"/collections/{self._collection_name}/points/scroll",
                json=scroll_body,
            )

            if resp.status_code >= 400:
                logger.error(
                    "Qdrant scroll for expired points failed: %d %s",
                    resp.status_code,
                    resp.text,
                )
                return 0

            data = resp.json()
            points = data.get("result", {}).get("points", [])

            if not points:
                return 0

            # Delete expired points
            point_ids = [p["id"] for p in points]
            delete_body = {"points": point_ids}

            resp = await self._client.post(
                f"/collections/{self._collection_name}/points/delete",
                json=delete_body,
            )

            if resp.status_code >= 400:
                logger.error(
                    "Qdrant delete expired points failed: %d %s",
                    resp.status_code,
                    resp.text,
                )
                return 0

            logger.info("Pruned %d expired entries from Qdrant", len(point_ids))
            return len(point_ids)

        except Exception:
            logger.exception("Qdrant prune_expired error")
            return 0

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    async def count(self) -> int:
        """Return the number of points in the collection."""
        if not self._available or self._client is None:
            return 0

        try:
            body = {"exact": True}
            resp = await self._client.post(
                f"/collections/{self._collection_name}/points/count",
                json=body,
            )

            if resp.status_code >= 400:
                logger.error(
                    "Qdrant count failed: %d %s",
                    resp.status_code,
                    resp.text,
                )
                return 0

            data = resp.json()
            return data.get("result", {}).get("count", 0)

        except Exception:
            logger.exception("Qdrant count error")
            return 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available
