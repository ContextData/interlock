"""FAISS-backed semantic index with read-copy-update pattern.

Uses IndexFlatIP (inner product on normalized vectors = cosine similarity).
Immutable for reads; writes go to a staging area. Background task periodically
rebuilds the index from staged data.

Gracefully degrades when faiss-cpu is not installed by falling back to a
pure-Python dot-product search (functionally identical for small datasets).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import msgpack

from interlock.models import SemanticMatch

logger = logging.getLogger(__name__)

FAISS_GENERATION_KEY_PREFIX = "onyx:faiss:generation:"

# ---------------------------------------------------------------------------
# Optional FAISS import
# ---------------------------------------------------------------------------

# The module when the `vector` extra is installed, None otherwise.
_faiss: Any = None
try:
    import faiss

    _faiss = faiss
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Optional Redis type for annotations
# ---------------------------------------------------------------------------

try:
    import redis.asyncio  # noqa: F401
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SemanticIndex(Protocol):
    """Protocol for semantic vector indexes."""

    async def search(
        self,
        embedding: list[float],
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[SemanticMatch]: ...

    async def upsert(
        self,
        key: str,
        embedding: list[float],
        metadata: dict,
        ttl_seconds: int | None = None,
    ) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def prune_expired(self) -> int: ...

    async def count(self) -> int: ...


# ---------------------------------------------------------------------------
# Pure-Python fallback helpers (no numpy/faiss dependency)
# ---------------------------------------------------------------------------


def _normalize(vec: list[float]) -> list[float]:
    """L2-normalize a vector in pure Python."""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm < 1e-12:
        return vec
    return [v / norm for v in vec]


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product of two vectors."""
    return sum(x * y for x, y in zip(a, b))


@dataclass(frozen=True)
class _FAISSGenerationSnapshot:
    """Immutable live view used by searchers."""

    generation: int
    index: Any | None
    keys: list[str]
    vectors: list[list[float]]
    metadata: dict[str, dict]
    ttls: dict[str, float]


# ---------------------------------------------------------------------------
# FAISSIndex
# ---------------------------------------------------------------------------


class FAISSIndex:
    """FAISS-backed semantic index with read-copy-update pattern.

    Uses IndexFlatIP (inner product on normalized vectors = cosine similarity).
    Falls back to pure-Python dot-product search when faiss-cpu is not installed.
    """

    def __init__(
        self,
        dimension: int = 384,
        namespace: str = "cache",
        redis_client: Any | None = None,
    ) -> None:
        self._dimension = dimension
        self._namespace = namespace
        self._redis = redis_client

        # In-memory live view. Rebuild creates a new snapshot and swaps this
        # reference once, so readers never observe partially swapped arrays.
        self._snapshot = _FAISSGenerationSnapshot(
            generation=0,
            index=None,
            keys=[],
            vectors=[],
            metadata={},
            ttls={},
        )
        self._loaded_redis_generation = 0

        # Staging area for writes (applied on next rebuild)
        self._staged_upserts: dict[str, tuple[list[float], dict, float | None]] = {}
        self._staged_deletes: set[str] = set()

        self._use_faiss = _faiss is not None
        self._available = False
        self._lock = asyncio.Lock()
        self._executor: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create the FAISS index. Falls back to pure-Python if faiss not installed."""
        if self._use_faiss:
            try:
                initial_index = _faiss.IndexFlatIP(self._dimension)
                self._snapshot = _FAISSGenerationSnapshot(
                    generation=self._snapshot.generation,
                    index=initial_index,
                    keys=[],
                    vectors=[],
                    metadata={},
                    ttls={},
                )
                self._executor = ThreadPoolExecutor(max_workers=1)
                self._available = True
                logger.info(
                    "FAISSIndex initialized (faiss backend, dim=%d, ns=%s)",
                    self._dimension,
                    self._namespace,
                )
            except Exception:
                logger.exception("Failed to create FAISS index")
                self._available = False
                return
        else:
            # Pure-Python fallback
            self._available = True
            logger.info(
                "FAISSIndex initialized (pure-Python fallback, dim=%d, ns=%s)",
                self._dimension,
                self._namespace,
            )

        # If Redis is available, reload persisted entries
        if self._redis is not None:
            await self.reload_from_redis()

    async def reload_from_redis(self) -> None:
        """Replace the live snapshot with all entries persisted in Redis."""
        if self._redis is None:
            return

        prefix = f"faiss:{self._namespace}:"
        try:
            staged: dict[str, tuple[list[float], dict, float | None]] = {}
            cursor = b"0"
            while True:
                cursor, keys = await self._redis.scan(cursor=cursor, match=f"{prefix}*", count=200)
                for raw_key in keys:
                    key_str = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                    short_key = key_str[len(prefix) :]
                    raw_val = await self._redis.get(raw_key)
                    if raw_val is None:
                        continue
                    data = msgpack.unpackb(raw_val, raw=False)
                    embedding = data["embedding"]
                    metadata = data.get("metadata", {})
                    ttl = data.get("ttl")
                    # Skip already-expired entries
                    if ttl is not None and ttl < time.time():
                        await self._redis.delete(raw_key)
                        continue
                    staged[short_key] = (embedding, metadata, ttl)
                if cursor == b"0" or cursor == 0:
                    break

            self._staged_deletes.clear()
            self._staged_upserts = staged
            await self.rebuild(replace=True)
            self._loaded_redis_generation = await self._current_redis_generation()
            logger.info(
                "Loaded %d entries from Redis for namespace %s generation=%d",
                len(self._snapshot.keys),
                self._namespace,
                self._loaded_redis_generation,
            )
        except Exception:
            logger.exception("Failed to load entries from Redis")

    async def reload_if_redis_generation_changed(self, *, force: bool = False) -> bool:
        """Reload from Redis when the persisted generation moved forward."""
        if self._redis is None:
            return False
        current = await self._current_redis_generation()
        if not force and current <= self._loaded_redis_generation:
            return False
        await self.reload_from_redis()
        return True

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        embedding: list[float],
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[SemanticMatch]:
        """Search for similar vectors. Returns matches sorted by score DESC.

        Filters can match on metadata keys (exact equality).
        Does NOT need a lock - reads an immutable snapshot.
        """
        snapshot = self._snapshot
        if not self._available or not snapshot.keys:
            return []

        query = _normalize(embedding)

        if self._use_faiss and snapshot.index is not None:
            matches = await self._search_faiss(snapshot, query, top_k, filters)
        else:
            matches = self._search_fallback(snapshot, query, top_k, filters)

        return matches

    async def _search_faiss(
        self,
        snapshot: _FAISSGenerationSnapshot,
        query: list[float],
        top_k: int,
        filters: dict | None,
    ) -> list[SemanticMatch]:
        """Search using FAISS (runs in executor since it is CPU-bound)."""
        import numpy as np

        query_array = np.array([query], dtype=np.float32)
        # Request more results to allow for post-filtering
        fetch_k = min(top_k * 4, len(snapshot.keys)) if filters else top_k
        fetch_k = max(fetch_k, 1)

        loop = asyncio.get_running_loop()
        scores, indices = await loop.run_in_executor(
            self._executor,
            lambda: snapshot.index.search(query_array, fetch_k),
        )

        results: list[SemanticMatch] = []
        now = time.time()
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(snapshot.keys):
                continue
            key = snapshot.keys[idx]
            # Skip expired entries
            if key in snapshot.ttls and snapshot.ttls[key] < now:
                continue
            meta = snapshot.metadata.get(key, {})
            if filters and not _metadata_matches(meta, filters):
                continue
            results.append(SemanticMatch(key=key, score=float(score), metadata=meta))
            if len(results) >= top_k:
                break

        return results

    def _search_fallback(
        self,
        snapshot: _FAISSGenerationSnapshot,
        query: list[float],
        top_k: int,
        filters: dict | None,
    ) -> list[SemanticMatch]:
        """Search using pure-Python dot product."""
        now = time.time()
        scored: list[tuple[float, int]] = []
        for i, vec in enumerate(snapshot.vectors):
            key = snapshot.keys[i]
            # Skip expired
            if key in snapshot.ttls and snapshot.ttls[key] < now:
                continue
            meta = snapshot.metadata.get(key, {})
            if filters and not _metadata_matches(meta, filters):
                continue
            score = _dot(query, vec)
            scored.append((score, i))

        # Sort descending by score
        scored.sort(key=lambda x: x[0], reverse=True)

        results: list[SemanticMatch] = []
        for score, i in scored[:top_k]:
            key = snapshot.keys[i]
            results.append(
                SemanticMatch(
                    key=key,
                    score=score,
                    metadata=snapshot.metadata.get(key, {}),
                )
            )
        return results

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
        """Add or update an entry. Goes to staging area.

        If Redis is available, also persists for recovery across restarts.
        """
        if not self._available:
            return

        expiry: float | None = None
        if ttl_seconds is not None:
            expiry = time.time() + ttl_seconds

        async with self._lock:
            self._staged_upserts[key] = (embedding, metadata, expiry)
            self._staged_deletes.discard(key)

        # Persist to Redis
        if self._redis is not None:
            redis_key = f"faiss:{self._namespace}:{key}"
            payload = msgpack.packb(
                {"embedding": embedding, "metadata": metadata, "ttl": expiry},
                use_bin_type=True,
            )
            try:
                if ttl_seconds is not None:
                    await self._redis.set(redis_key, payload, ex=ttl_seconds)
                else:
                    await self._redis.set(redis_key, payload)
                self._loaded_redis_generation = await self._bump_redis_generation()
            except Exception:
                logger.exception("Failed to persist entry %s to Redis", key)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, key: str) -> None:
        """Delete a key and make it unsearchable immediately."""
        if not self._available:
            return

        async with self._lock:
            self._staged_deletes.add(key)
            self._staged_upserts.pop(key, None)

        # Remove from Redis
        if self._redis is not None:
            redis_key = f"faiss:{self._namespace}:{key}"
            try:
                await self._redis.delete(redis_key)
                self._loaded_redis_generation = await self._bump_redis_generation()
            except Exception:
                logger.exception("Failed to delete entry %s from Redis", key)

        # A staged-only delete leaves the active FAISS snapshot searchable until
        # the next background rebuild. Governance/cache invalidation requires a
        # deleted semantic entry to disappear from active search immediately.
        await self.rebuild()

    # ------------------------------------------------------------------
    # Rebuild (read-copy-update)
    # ------------------------------------------------------------------

    async def rebuild(self, *, replace: bool = False) -> None:
        """Rebuild the index from current data + staged changes.

        Build a new index, then atomically swap the reference.
        """
        if not self._available:
            return

        async with self._lock:
            # Merge current data with staged changes
            merged: dict[str, tuple[list[float], dict, float | None]] = {}

            # Start with existing entries
            if not replace:
                snapshot = self._snapshot
                for i, key in enumerate(snapshot.keys):
                    vec = snapshot.vectors[i]
                    meta = snapshot.metadata.get(key, {})
                    ttl = snapshot.ttls.get(key)
                    merged[key] = (vec, meta, ttl)

            # Apply staged deletes
            for key in self._staged_deletes:
                merged.pop(key, None)

            # Apply staged upserts (overwrite existing)
            for key, (emb, meta, ttl) in self._staged_upserts.items():
                merged[key] = (emb, meta, ttl)

            # Build new parallel arrays
            new_keys: list[str] = []
            new_vectors: list[list[float]] = []
            new_metadata: dict[str, dict] = {}
            new_ttls: dict[str, float] = {}

            for key, (emb, meta, ttl) in merged.items():
                norm_emb = _normalize(emb)
                new_keys.append(key)
                new_vectors.append(norm_emb)
                new_metadata[key] = meta
                if ttl is not None:
                    new_ttls[key] = ttl

            # Build new FAISS index if using faiss
            new_index = None
            if self._use_faiss and _faiss is not None:
                import numpy as np

                new_index = _faiss.IndexFlatIP(self._dimension)
                if new_vectors:
                    matrix = np.array(new_vectors, dtype=np.float32)
                    new_index.add(matrix)

            # Atomic generation swap. All fields move together through one
            # immutable snapshot object captured by searchers.
            self._snapshot = _FAISSGenerationSnapshot(
                generation=self._snapshot.generation + 1,
                index=new_index,
                keys=new_keys,
                vectors=new_vectors,
                metadata=new_metadata,
                ttls=new_ttls,
            )

            # Clear staging
            self._staged_upserts.clear()
            self._staged_deletes.clear()

    # ------------------------------------------------------------------
    # Prune expired
    # ------------------------------------------------------------------

    async def prune_expired(self) -> int:
        """Remove entries whose TTL has expired. Returns count removed."""
        if not self._available:
            return 0

        now = time.time()
        expired_keys = [k for k, exp in self._snapshot.ttls.items() if exp < now]

        if not expired_keys:
            return 0

        for key in expired_keys:
            async with self._lock:
                self._staged_deletes.add(key)
                self._staged_upserts.pop(key, None)

            # Clean up Redis
            if self._redis is not None:
                redis_key = f"faiss:{self._namespace}:{key}"
                try:
                    await self._redis.delete(redis_key)
                    self._loaded_redis_generation = await self._bump_redis_generation()
                except Exception:
                    logger.exception("Failed to delete expired entry %s from Redis", key)

        await self.rebuild()
        return len(expired_keys)

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    async def count(self) -> int:
        """Return number of indexed vectors (excludes staged-only entries)."""
        return len(self._snapshot.keys)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    @property
    def generation(self) -> int:
        return self._snapshot.generation

    @property
    def redis_generation(self) -> int:
        return self._loaded_redis_generation

    @property
    def namespace(self) -> str:
        return self._namespace

    async def freshness(self) -> dict[str, int | bool]:
        """Return whether the immutable snapshot has caught up to Redis."""
        remote = await self._current_redis_generation()
        return {
            "loaded_generation": self._loaded_redis_generation,
            "remote_generation": remote,
            "fresh": remote <= self._loaded_redis_generation,
        }

    @property
    def _index(self) -> Any | None:
        return self._snapshot.index

    @property
    def _keys(self) -> list[str]:
        return self._snapshot.keys

    @property
    def _vectors(self) -> list[list[float]]:
        return self._snapshot.vectors

    @property
    def _metadata(self) -> dict[str, dict]:
        return self._snapshot.metadata

    @property
    def _ttls(self) -> dict[str, float]:
        return self._snapshot.ttls

    def redis_generation_gap(self, incoming_generation: int) -> int:
        return max(0, int(incoming_generation) - self._loaded_redis_generation)

    async def _current_redis_generation(self) -> int:
        if self._redis is None:
            return self._loaded_redis_generation
        try:
            raw = await self._redis.get(self._generation_key())
            if raw is None:
                return 0
            if isinstance(raw, bytes):
                raw = raw.decode("ascii")
            return int(raw)
        except Exception:
            logger.warning(
                "Failed to read FAISS Redis generation namespace=%s",
                self._namespace,
                exc_info=True,
            )
            return self._loaded_redis_generation

    async def _bump_redis_generation(self) -> int:
        if self._redis is None:
            return self._loaded_redis_generation
        try:
            generation = int(await self._redis.incr(self._generation_key()))
            await self._redis.publish(
                "onyx:faiss_rebuild",
                json.dumps(
                    {"namespace": self._namespace, "generation": generation},
                ),
            )
            return generation
        except Exception:
            logger.warning(
                "Failed to bump FAISS Redis generation namespace=%s",
                self._namespace,
                exc_info=True,
            )
            return self._loaded_redis_generation

    def _generation_key(self) -> str:
        return f"{FAISS_GENERATION_KEY_PREFIX}{self._namespace}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metadata_matches(metadata: dict, filters: dict) -> bool:
    """Check if metadata satisfies all filter conditions (exact equality)."""
    for key, value in filters.items():
        if key not in metadata or metadata[key] != value:
            return False
    return True
