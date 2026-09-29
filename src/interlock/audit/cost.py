"""Usage attribution - what governed requests did, by identity, source and team.

This used to report a weighted "cost unit" per audit row (1.0 per request, 0.1
per cache hit, 10.0 for an LLM check that nothing ever logged), and the console
showed it next to the request count as if it were money. InterLock does not
price requests, so it now counts them by outcome instead:

  upstream  served by the source
  cached    served from InterLock's cache, without reaching the source
  denied    refused by a source role or policy
  queued    held for write approval
  errors    failed for any other reason

Real upstream cost reported by a connector (Snowflake credits, bytes scanned)
is kept per request in `audit_log.cost_metadata` and shown on the audit event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from interlock.admin.identity_labels import IDENTITY_NAME_COLUMNS, identity_joins

OUTCOMES: tuple[tuple[str, str], ...] = (
    ("upstream", "Served by the source"),
    ("cached", "Served from cache"),
    ("denied", "Denied"),
    ("queued", "Queued for approval"),
    ("errors", "Errors"),
)

# Selectable ranges for the console, in the order shown.
RANGES: dict[str, tuple[str, timedelta]] = {
    "24h": ("Last 24 hours", timedelta(hours=24)),
    "7d": ("Last 7 days", timedelta(days=7)),
    "30d": ("Last 30 days", timedelta(days=30)),
}
DEFAULT_RANGE = "7d"

_OUTCOME_COLUMNS = """
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE a.status = 'success' AND NOT COALESCE(a.cache_hit, FALSE)) AS upstream,
    COUNT(*) FILTER (WHERE a.status = 'success' AND COALESCE(a.cache_hit, FALSE)) AS cached,
    COUNT(*) FILTER (WHERE a.status = 'denied') AS denied,
    COUNT(*) FILTER (WHERE a.status = 'queued') AS queued,
    COUNT(*) FILTER (
        WHERE a.status IS NULL OR a.status NOT IN ('success', 'denied', 'queued')
    ) AS errors
"""


@dataclass(frozen=True, slots=True)
class Segment:
    outcome: str
    count: int
    x: float
    width: float


def bar_segments(row: dict[str, Any], scale: int) -> list[Segment]:
    """Stacked-bar segments for one row, as percentages of the largest row."""
    segments: list[Segment] = []
    offset = 0.0
    for outcome, _label in OUTCOMES:
        count = int(row.get(outcome) or 0)
        width = 100.0 * count / scale if scale else 0.0
        if count:
            segments.append(Segment(outcome, count, round(offset, 3), round(width, 3)))
        offset += width
    return segments


def since_for(range_key: str, now: datetime) -> datetime:
    return now - RANGES.get(range_key, RANGES[DEFAULT_RANGE])[1]


class UsageAggregator:
    """Counts audit rows by outcome for a time window."""

    def __init__(self, pg_pool: Any) -> None:
        self._pool = pg_pool

    async def summary(self, since: datetime) -> dict[str, Any]:
        row = await self._pool.fetchrow(
            f"""
            SELECT {_OUTCOME_COLUMNS},
                   COALESCE(AVG(a.latency_ms), 0) AS avg_latency_ms
            FROM audit_log a
            WHERE a.created_at >= $1
            """,
            since,
        )
        data = dict(row) if row is not None else {}
        summary: dict[str, Any] = {
            key: int(data.get(key) or 0) for key in ("total", *dict(OUTCOMES))
        }
        served = summary["upstream"] + summary["cached"]
        summary["cache_hit_rate"] = summary["cached"] / served if served else 0.0
        summary["avg_latency_ms"] = float(data.get("avg_latency_ms") or 0)
        return summary

    async def by_identity(self, since: datetime, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            f"""
            SELECT a.identity_id, {IDENTITY_NAME_COLUMNS}, {_OUTCOME_COLUMNS}
            FROM audit_log a
            {identity_joins("a.identity_id")}
            WHERE a.created_at >= $1
            GROUP BY a.identity_id, i.id, i.name, it.identity_id, it.name
            ORDER BY total DESC
            LIMIT $2
            """,
            since,
            limit,
        )
        return [dict(r) for r in rows]

    async def by_source(self, since: datetime, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            f"""
            SELECT a.source_id, {_OUTCOME_COLUMNS}
            FROM audit_log a
            WHERE a.created_at >= $1
            GROUP BY a.source_id
            ORDER BY total DESC
            LIMIT $2
            """,
            since,
            limit,
        )
        return [dict(r) for r in rows]

    async def by_team(self, since: datetime, limit: int = 10) -> list[dict[str, Any]]:
        """Per team; requests with no team (or no identity) are one row, not dropped."""
        rows = await self._pool.fetch(
            f"""
            SELECT COALESCE(i.team, it.team) AS team, {_OUTCOME_COLUMNS}
            FROM audit_log a
            {identity_joins("a.identity_id")}
            WHERE a.created_at >= $1
            GROUP BY COALESCE(i.team, it.team)
            ORDER BY total DESC
            LIMIT $2
            """,
            since,
            limit,
        )
        return [dict(r) for r in rows]
