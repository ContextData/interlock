"""Discovery vector generation publish/reload helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

FAISS_REBUILD_CHANNEL = "onyx:faiss_rebuild"
GENERATION_KEY_PREFIX = "onyx:faiss:generation:"
_LOADED_GENERATION_ATTR = "_onyx_loaded_generation"
_RELOAD_LOCK_ATTR = "_onyx_generation_reload_lock"


async def publish_vector_generation(
    redis_client: Any | None,
    *,
    namespace: str,
    source_id: str,
    asset_path: str,
    asset_id: int,
) -> int | None:
    """Increment and publish a vector generation for peer index reloads."""
    if redis_client is None:
        return None

    key = f"{GENERATION_KEY_PREFIX}{namespace}"
    try:
        generation = await redis_client.incr(key)
        payload = json.dumps(
            {
                "version": 1,
                "event": "index_updated",
                "namespace": namespace,
                "generation": int(generation),
                "source_id": source_id,
                "asset_path": asset_path,
                "asset_id": asset_id,
            }
        )
        await redis_client.publish(FAISS_REBUILD_CHANNEL, payload)
        return int(generation)
    except Exception:
        logger.warning(
            "Failed to publish vector generation namespace=%s source=%s path=%s",
            namespace,
            source_id,
            asset_path,
            exc_info=True,
        )
        return None


async def reload_index_if_generation_changed(index: Any | None, *, namespace: str) -> bool:
    """Reload a FAISS-like index from Redis when another process published new vectors."""
    if index is None:
        return False
    redis_client = getattr(index, "_redis", None)
    if redis_client is None:
        return False

    current = await _current_generation(redis_client, namespace)
    if current is None:
        return False

    loaded = int(getattr(index, _LOADED_GENERATION_ATTR, 0) or 0)
    if current <= loaded:
        return False

    lock = getattr(index, _RELOAD_LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(index, _RELOAD_LOCK_ATTR, lock)

    async with lock:
        loaded = int(getattr(index, _LOADED_GENERATION_ATTR, 0) or 0)
        if current <= loaded:
            return False

        loader = getattr(index, "_load_from_redis", None)
        if callable(loader):
            await loader()
        else:
            rebuild = getattr(index, "rebuild", None)
            if not callable(rebuild):
                return False
            await rebuild()
        setattr(index, _LOADED_GENERATION_ATTR, current)
        logger.info("Reloaded %s vector index at generation %d", namespace, current)
        return True


async def _current_generation(redis_client: Any, namespace: str) -> int | None:
    try:
        raw = await redis_client.get(f"{GENERATION_KEY_PREFIX}{namespace}")
    except Exception:
        logger.warning("Failed to read vector generation namespace=%s", namespace, exc_info=True)
        return None
    if raw is None:
        return 0
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
