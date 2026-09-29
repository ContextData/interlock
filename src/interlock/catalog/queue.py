"""Queueing a catalog scan.

Every trigger - a save, the Rescan button, the API, the scheduler - comes
through `enqueue_catalog_scan`. It is idempotent: the partial unique index on
pending scans means a source has at most one waiting scan, so a burst of saves,
or two workers' schedulers ticking together, queue one scan rather than many.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from interlock.catalog.collectors import has_collector

logger = logging.getLogger(__name__)

TRIGGERS = frozenset({"save", "manual", "scheduled", "startup", "api"})

# A request for a scan that is already waiting joins it rather than queueing a
# second, but brings it forward: a Rescan click must not sit behind a scheduled
# scan's jitter. A person's request also replaces the scheduler's attribution,
# so the history shows who asked.
_ENQUEUE_SQL = """
INSERT INTO source_catalog_scans (source_id, trigger, requested_by, not_before)
VALUES ($1, $2, $3, NOW() + make_interval(secs => $4))
ON CONFLICT (source_id) WHERE status = 'pending' DO UPDATE SET
    not_before = LEAST(source_catalog_scans.not_before, EXCLUDED.not_before),
    trigger = CASE WHEN source_catalog_scans.trigger = 'scheduled'
                   THEN EXCLUDED.trigger ELSE source_catalog_scans.trigger END,
    requested_by = COALESCE(source_catalog_scans.requested_by, EXCLUDED.requested_by)
RETURNING id
"""


async def enqueue_catalog_scan(
    pool: Any,
    source_id: str,
    *,
    trigger: str,
    requested_by: str | None = None,
    delay_seconds: float = 0,
) -> int | None:
    """Queue a scan of `source_id` and return the pending scan's id.

    Returns the id of the scan already waiting when one exists, and `None` for a
    source that is disabled, unknown, or of a connector with no collector.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown catalog scan trigger: {trigger}")

    row = await pool.fetchrow(
        "SELECT source_type, enabled, metadata FROM data_sources WHERE source_id = $1",
        source_id,
    )
    if row is None or not row["enabled"]:
        return None
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    if not isinstance(metadata, dict):
        metadata = {}
    connector_key = str(
        metadata.get("connector_key") or metadata.get("provider") or row["source_type"]
    )
    if not has_collector(connector_key):
        return None

    scan_id = await pool.fetchval(
        _ENQUEUE_SQL, source_id, trigger, requested_by, float(max(delay_seconds, 0))
    )
    if scan_id is None:
        scan_id = await pool.fetchval(
            "SELECT id FROM source_catalog_scans WHERE source_id = $1 AND status = 'pending'",
            source_id,
        )
    return int(scan_id) if scan_id is not None else None


async def enqueue_after_save(
    pool: Any, source_id: str, *, requested_by: str | None = None
) -> int | None:
    """What every source save path calls. Never raises into the save.

    A catalog that could not be queued is a degraded catalog, not a failed
    save: the source is already stored, and the next scheduled refresh or a
    manual Rescan will catch it up.
    """
    try:
        return await enqueue_catalog_scan(
            pool, source_id, trigger="save", requested_by=requested_by, delay_seconds=5
        )
    except Exception:
        logger.warning("Could not queue a catalog scan for source %s", source_id, exc_info=True)
        return None


async def enqueue_many(
    pool: Any,
    *,
    trigger: str,
    requested_by: str | None = None,
    only_never_scanned: bool = False,
) -> tuple[list[str], list[str]]:
    """Queue a scan for every enabled source. Returns (queued, skipped) ids.

    Skipped sources are those with no collector. With `only_never_scanned`,
    sources that already have any scan, finished or not, are left alone: the
    admin's startup pass exists to seed a catalog, not to refresh one - that is
    the scheduler's job.
    """
    rows = await pool.fetch(
        """
        SELECT ds.source_id
        FROM data_sources ds
        WHERE ds.enabled
          AND (NOT $1 OR NOT EXISTS (
              SELECT 1 FROM source_catalog_scans s WHERE s.source_id = ds.source_id
          ))
        ORDER BY ds.source_id
        """,
        only_never_scanned,
    )
    queued: list[str] = []
    skipped: list[str] = []
    for row in rows:
        source_id = str(row["source_id"])
        scan_id = await enqueue_catalog_scan(
            pool, source_id, trigger=trigger, requested_by=requested_by
        )
        (queued if scan_id is not None else skipped).append(source_id)
    return queued, skipped
