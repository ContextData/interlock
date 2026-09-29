"""Cache invalidation on writes.

Provides a CacheInvalidator that clears cache entries across all tiers
when a write operation modifies underlying data. Publishes invalidation
events to Redis pub/sub for cross-instance coordination.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from interlock.cache.faiss_index import FAISSIndex
from interlock.cache.generation import SourceGenerationBarrier, SourceGenerationToken
from interlock.cache.l1 import L1Cache
from interlock.cache.l2 import L2Cache

logger = logging.getLogger(__name__)

# Redis pub/sub channel for cross-instance invalidation
INVALIDATION_CHANNEL = "onyx:cache_invalidate"

# Optional Redis import for type hints
try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore[assignment]


class CacheInvalidator:
    """Invalidate cache entries across all tiers on data writes.

    For L1/L2: invalidates dependency-tracked keys when available, with a
    conservative clear fallback for untracked legacy entries.
    For semantic: searches by metadata.source_id and deletes matching entries.
    Publishes invalidation events to Redis pub/sub for cross-instance sync.
    """

    def __init__(
        self,
        l1: L1Cache | None = None,
        l2: L2Cache | None = None,
        semantic_index: FAISSIndex | None = None,
        redis_client: Any | None = None,
        pg_pool: Any | None = None,
        instance_id: str | None = None,
        source_generation_barrier: SourceGenerationBarrier | None = None,
        reconnect_backoff_seconds: float = 1.0,
    ) -> None:
        self._l1 = l1
        self._l2 = l2
        self._semantic_index = semantic_index
        self._redis = redis_client
        self._pool = pg_pool
        self._instance_id = instance_id
        self._source_barrier = source_generation_barrier or SourceGenerationBarrier(redis_client)
        self._reconnect_backoff = reconnect_backoff_seconds
        self._listener_task: asyncio.Task | None = None
        self._redis_pubsub: Any | None = None
        self._local_source_index: dict[str, set[str]] = {}
        self._local_table_index: dict[tuple[str, str], set[str]] = {}
        self._local_asset_index: dict[tuple[str, str], set[str]] = {}
        self._last_seen_source_generation: dict[str, int] = {}

    async def prepare_source_write(self, source_id: str) -> SourceGenerationToken:
        """Advance source generation before a write and return a barrier token."""
        token = await self._source_barrier.advance(source_id)
        self._last_seen_source_generation[source_id] = max(
            token.generation,
            self._last_seen_source_generation.get(source_id, 0),
        )
        return token

    async def prepare_shared_source_write(self, source_id: str) -> SourceGenerationToken:
        token = await self._source_barrier.advance_shared(source_id)
        self._last_seen_source_generation[source_id] = max(
            token.generation,
            self._last_seen_source_generation.get(source_id, 0),
        )
        return token

    async def shared_source_generation(self, source_id: str) -> int:
        current = await self._source_barrier.current_shared(source_id)
        return max(current, self._last_seen_source_generation.get(source_id, 0))

    async def source_generation(self, source_id: str) -> int:
        """Return the latest source generation known to this invalidator."""
        current = await self._source_barrier.current(source_id)
        return max(current, self._last_seen_source_generation.get(source_id, 0))

    async def wait_for_source_generation(
        self,
        token: SourceGenerationToken,
        *,
        timeout_seconds: float = 1.0,
    ) -> bool:
        """Wait until the source-generation barrier reaches a token."""
        return await self._source_barrier.wait_until_at_least(
            token,
            timeout_seconds=timeout_seconds,
        )

    async def record_dependency(
        self,
        cache_key: str,
        *,
        source_id: str,
        tables: list[str] | None = None,
        asset_path: str | None = None,
        protocol: str | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        """Record source/table/asset dimensions for a deterministic cache key."""
        tables = sorted({table for table in (tables or []) if table})
        metadata = metadata or {}
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        dimensions: list[str | None] = list(tables) if tables else [None]

        self._local_source_index.setdefault(source_id, set()).add(cache_key)
        for table in tables:
            self._local_table_index.setdefault((source_id, table), set()).add(cache_key)
        if asset_path:
            self._local_asset_index.setdefault((source_id, asset_path), set()).add(cache_key)

        if self._pool is None:
            return

        try:
            for table_dim in dimensions:
                await self._pool.execute(
                    """
                    DELETE FROM cache_dependencies
                    WHERE cache_key = $1
                      AND source_id = $2
                      AND COALESCE(table_name, '') = COALESCE($3::text, '')
                      AND COALESCE(asset_path, '') = COALESCE($4::text, '')
                    """,
                    cache_key,
                    source_id,
                    table_dim,
                    asset_path,
                )
                await self._pool.execute(
                    """
                    INSERT INTO cache_dependencies
                        (cache_key, source_id, table_name, asset_path, protocol, expires_at, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    cache_key,
                    source_id,
                    table_dim,
                    asset_path,
                    protocol,
                    expires_at,
                    metadata,
                )
        except Exception:
            logger.warning(
                "Failed to record cache dependency key=%s source=%s tables=%s asset=%s",
                cache_key,
                source_id,
                tables,
                asset_path,
                exc_info=True,
            )

    async def invalidate_for_tables(
        self,
        source_id: str,
        tables: list[str],
        *,
        source_generation: int | None = None,
    ) -> int:
        """Invalidate all cache entries related to the given tables.

        For L1/L2: invalidates dependency-tracked deterministic keys when
        available and falls back to a full deterministic clear for untracked
        legacy entries.
        For semantic: search by metadata.source_id and delete matches.
        Publishes invalidation event to Redis pub/sub for cross-instance sync.

        Returns count of entries invalidated.
        """
        keys = await self._dependency_keys(source_id=source_id, tables=tables)
        count = await self._invalidate_deterministic_keys(keys)
        if not keys:
            count += await self._clear_deterministic_fallback()

        # Semantic: search for entries matching source_id and delete
        count += await self._invalidate_semantic(source_id)

        await self._delete_dependencies(source_id=source_id, tables=tables, cache_keys=keys)

        # Publish invalidation event
        await self.publish_invalidation(
            source_id,
            tables,
            cache_keys=sorted(keys),
            source_generation=source_generation,
        )

        logger.info(
            "Invalidated %d entries for source=%s tables=%s",
            count,
            source_id,
            tables,
        )
        return count

    async def invalidate_for_source(self, source_id: str) -> int:
        """Invalidate all cache entries for a data source.

        Clears L1 entirely and removes semantic entries matching source_id.
        """
        keys = await self._dependency_keys(source_id=source_id)
        count = await self._invalidate_deterministic_keys(keys)
        if not keys:
            count += await self._clear_deterministic_fallback()

        # Semantic: delete entries matching source_id
        count += await self._invalidate_semantic(source_id)

        await self._delete_dependencies(source_id=source_id, cache_keys=keys)

        # Publish
        await self.publish_invalidation(source_id, tables=[], cache_keys=sorted(keys))

        logger.info(
            "Invalidated %d entries for source=%s",
            count,
            source_id,
        )
        return count

    async def publish_invalidation(
        self,
        source_id: str,
        tables: list[str],
        *,
        cache_keys: list[str] | None = None,
        source_generation: int | None = None,
    ) -> None:
        """Publish invalidation event to Redis onyx:cache_invalidate channel."""
        if self._redis is None:
            return
        generation = source_generation
        if generation is None:
            generation = (await self.prepare_source_write(source_id)).generation
        else:
            await self._source_barrier.mark_seen(
                SourceGenerationToken(source_id=source_id, generation=generation)
            )
            self._last_seen_source_generation[source_id] = max(
                generation,
                self._last_seen_source_generation.get(source_id, 0),
            )

        message = json.dumps(
            {
                "version": 2,
                "source_id": source_id,
                "tables": tables,
                "cache_keys": cache_keys or [],
                "generation": generation,
                "source_instance": self._instance_id,
            }
        )
        try:
            await self._redis.publish(INVALIDATION_CHANNEL, message)
        except Exception:
            logger.warning(
                "Failed to publish invalidation event for source=%s",
                source_id,
                exc_info=True,
            )

    async def start_listener(self) -> None:
        """Subscribe to Redis invalidation events from peer gateway instances."""
        if self._redis is None or self._listener_task is not None:
            return
        self._listener_task = asyncio.create_task(
            self._listen_for_invalidations(),
            name="cache-invalidation-listener",
        )

    @property
    def listener_healthy(self) -> bool:
        return self._redis is None or (
            self._listener_task is not None and not self._listener_task.done()
        )

    async def stop_listener(self) -> None:
        """Stop the Redis invalidation listener."""
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        if self._redis_pubsub is not None:
            await self._close_pubsub()

    async def apply_remote_invalidation(self, payload: dict[str, Any]) -> int:
        """Apply an invalidation event received over Redis pub/sub."""
        if payload.get("source_instance") == self._instance_id:
            return 0
        source_id = str(payload.get("source_id") or "")
        tables = [str(table) for table in payload.get("tables") or [] if table]
        keys = {str(key) for key in payload.get("cache_keys") or [] if key}
        generation = _coerce_generation(payload.get("generation"))
        if source_id and generation is not None:
            last_seen = self._last_seen_source_generation.get(source_id, 0)
            await self._source_barrier.mark_seen(
                SourceGenerationToken(source_id=source_id, generation=generation)
            )
            self._last_seen_source_generation[source_id] = max(generation, last_seen)
            if generation > last_seen + 1:
                logger.warning(
                    "Cache invalidation generation gap source=%s last=%d incoming=%d; full resync",
                    source_id,
                    last_seen,
                    generation,
                )
                return await self._full_resync_source(source_id)
        if not keys and source_id:
            keys = await self._dependency_keys(source_id=source_id, tables=tables)

        count = await self._invalidate_deterministic_keys(keys)
        if not keys and source_id:
            count += await self._clear_deterministic_fallback()
        if source_id:
            count += await self._invalidate_semantic(source_id)
        return count

    # -- internal helpers ---------------------------------------------------

    async def _listen_for_invalidations(self) -> None:
        while True:
            try:
                await self._ensure_pubsub()
                assert self._redis_pubsub is not None
                message = await self._redis_pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if not message:
                    await asyncio.sleep(0)
                    continue
                raw = message.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if not isinstance(raw, str):
                    continue
                await self.apply_remote_invalidation(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Cache invalidation listener error; reconnecting", exc_info=True)
                await self._close_pubsub()
                await asyncio.sleep(self._reconnect_backoff)

    async def _ensure_pubsub(self) -> None:
        if self._redis_pubsub is not None:
            return
        assert self._redis is not None
        self._redis_pubsub = self._redis.pubsub()
        await self._redis_pubsub.subscribe(INVALIDATION_CHANNEL)

    async def _close_pubsub(self) -> None:
        if self._redis_pubsub is None:
            return
        try:
            await self._redis_pubsub.unsubscribe(INVALIDATION_CHANNEL)
            await self._redis_pubsub.aclose()
        finally:
            self._redis_pubsub = None

    async def _dependency_keys(
        self,
        *,
        source_id: str,
        tables: list[str] | None = None,
        asset_paths: list[str] | None = None,
    ) -> set[str]:
        tables = sorted({table for table in (tables or []) if table})
        asset_paths = sorted({asset for asset in (asset_paths or []) if asset})
        keys = set(self._local_source_index.get(source_id, set()))
        if tables:
            keys = set()
            for table in tables:
                keys.update(self._local_table_index.get((source_id, table), set()))
        if asset_paths:
            if not tables:
                keys = set()
            for asset_path in asset_paths:
                keys.update(self._local_asset_index.get((source_id, asset_path), set()))

        if self._pool is None:
            return keys

        try:
            await self._pool.execute(
                "DELETE FROM cache_dependencies WHERE expires_at IS NOT NULL AND expires_at <= NOW()"
            )
            conditions = ["source_id = $1"]
            params: list[Any] = [source_id]
            idx = 2
            if tables:
                conditions.append(f"table_name = ANY(${idx}::text[])")
                params.append(tables)
                idx += 1
            if asset_paths:
                conditions.append(f"asset_path = ANY(${idx}::text[])")
                params.append(asset_paths)
            sql = f"""
                SELECT DISTINCT cache_key
                FROM cache_dependencies
                WHERE {' AND '.join(conditions)}
            """
            rows = await self._pool.fetch(sql, *params)
            keys.update(str(row["cache_key"]) for row in rows)
        except Exception:
            logger.warning(
                "Failed to load cache dependency keys source=%s tables=%s assets=%s",
                source_id,
                tables,
                asset_paths,
                exc_info=True,
            )
        return keys

    async def _invalidate_deterministic_keys(self, keys: set[str]) -> int:
        count = 0
        for key in keys:
            if self._l1 is not None:
                await self._l1.invalidate(key)
                count += 1
            if self._l2 is not None:
                await self._l2.invalidate(key)
                count += 1
            self._remove_local_key(key)
        return count

    async def _clear_deterministic_fallback(self) -> int:
        count = 0
        if self._l1 is not None:
            size_before = self._l1.stats["size"]
            await self._l1.clear()
            count += size_before
        if self._l2 is not None:
            count += await self._l2.clear()
        self._local_source_index.clear()
        self._local_table_index.clear()
        self._local_asset_index.clear()
        return count

    async def _full_resync_source(self, source_id: str) -> int:
        """Conservative local resync used when Redis pub/sub generations gap."""
        count = await self._clear_deterministic_fallback()
        count += await self._invalidate_semantic(source_id)
        await self._delete_dependencies(source_id=source_id)
        return count

    async def _delete_dependencies(
        self,
        *,
        source_id: str,
        tables: list[str] | None = None,
        cache_keys: set[str] | None = None,
    ) -> None:
        if self._pool is None:
            return
        try:
            if cache_keys:
                await self._pool.execute(
                    "DELETE FROM cache_dependencies WHERE cache_key = ANY($1::text[])",
                    sorted(cache_keys),
                )
            elif tables:
                await self._pool.execute(
                    "DELETE FROM cache_dependencies WHERE source_id = $1 AND table_name = ANY($2::text[])",
                    source_id,
                    tables,
                )
            else:
                await self._pool.execute(
                    "DELETE FROM cache_dependencies WHERE source_id = $1",
                    source_id,
                )
        except Exception:
            logger.warning("Failed to delete cache dependencies", exc_info=True)

    def _remove_local_key(self, cache_key: str) -> None:
        for index in (
            self._local_source_index,
            self._local_table_index,
            self._local_asset_index,
        ):
            empty_keys = []
            for dep_key, values in index.items():
                values.discard(cache_key)
                if not values:
                    empty_keys.append(dep_key)
            for dep_key in empty_keys:
                index.pop(dep_key, None)

    async def _invalidate_semantic(self, source_id: str) -> int:
        """Search semantic index for entries matching source_id and delete them.

        Uses a zero-vector search with source_id filter to find all entries,
        then deletes each one. Returns count of entries deleted.
        """
        if self._semantic_index is None:
            return 0

        # Search with source_id filter to find matching entries.
        # We use a large top_k to catch all entries for this source.
        # The zero vector is not ideal but we need to find by metadata,
        # so we search with a dummy embedding and rely on the filter.
        dim = getattr(self._semantic_index, "_dimension", 384)
        dummy_embedding = [0.0] * dim

        try:
            matches = await self._semantic_index.search(
                embedding=dummy_embedding,
                top_k=10000,
                filters={"source_id": source_id},
            )
        except Exception:
            logger.warning(
                "Failed to search semantic index for source=%s",
                source_id,
                exc_info=True,
            )
            return 0

        deleted = 0
        for match in matches:
            try:
                await self._semantic_index.delete(match.key)
                deleted += 1
            except Exception:
                logger.warning(
                    "Failed to delete semantic entry key=%s",
                    match.key,
                    exc_info=True,
                )

        return deleted


def _coerce_generation(value: Any) -> int | None:
    if value is None:
        return None
    try:
        generation = int(value)
    except (TypeError, ValueError):
        return None
    return generation if generation >= 0 else None
