"""Usage attribution: requests counted by outcome, never priced."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.audit.cost import (
    DEFAULT_RANGE,
    OUTCOMES,
    RANGES,
    UsageAggregator,
    bar_segments,
    since_for,
)


def _pool() -> MagicMock:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


def test_outcomes_partition_every_status() -> None:
    assert [key for key, _ in OUTCOMES] == ["upstream", "cached", "denied", "queued", "errors"]


@pytest.mark.asyncio
async def test_summary_counts_outcomes_and_cache_rate_over_served_requests() -> None:
    pool = _pool()
    pool.fetchrow.return_value = {
        "total": 44,
        "upstream": 20,
        "cached": 12,
        "denied": 8,
        "queued": 1,
        "errors": 3,
        "avg_latency_ms": 19.2,
    }
    since = datetime(2026, 9, 18, tzinfo=UTC)

    summary = await UsageAggregator(pool).summary(since)

    assert summary["total"] == 44
    assert summary["denied"] == 8
    # Denied and failed requests were never served, so they do not dilute the rate.
    assert summary["cache_hit_rate"] == pytest.approx(12 / 32)
    assert summary["avg_latency_ms"] == pytest.approx(19.2)
    assert pool.fetchrow.await_args.args[1] == since


@pytest.mark.asyncio
async def test_summary_of_an_empty_window_is_zero() -> None:
    summary = await UsageAggregator(_pool()).summary(datetime.now(UTC))
    assert summary["total"] == 0 and summary["cache_hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_the_queries_count_outcomes_and_never_weight_rows() -> None:
    pool = _pool()
    agg = UsageAggregator(pool)
    since = datetime.now(UTC)
    await agg.by_identity(since)
    await agg.by_source(since)
    await agg.by_team(since)
    await agg.summary(since)
    queries = [c.args[0] for c in pool.fetch.await_args_list] + [pool.fetchrow.await_args.args[0]]
    for sql in queries:
        assert "COUNT(*) FILTER (WHERE a.status = 'denied') AS denied" in sql
        assert "AS cached" in sql and "AS upstream" in sql
        assert "0.1" not in sql and "10.0" not in sql
        assert "a.created_at >= $1" in sql


@pytest.mark.asyncio
async def test_identities_are_named_and_teams_keep_rows_without_one() -> None:
    pool = _pool()
    agg = UsageAggregator(pool)
    await agg.by_identity(datetime.now(UTC))
    await agg.by_team(datetime.now(UTC))
    identity_sql, team_sql = (c.args[0] for c in pool.fetch.await_args_list)
    assert "identity_tombstones" in identity_sql and "AS identity_name" in identity_sql
    # A LEFT JOIN: requests from no identity or a team-less one form a row
    # instead of dropping out, so team totals add up to the request total.
    assert "LEFT JOIN identities" in team_sql
    assert "JOIN identities i ON" in team_sql and "INNER" not in team_sql


def test_bar_segments_stack_in_outcome_order_scaled_to_the_busiest_row() -> None:
    row = {"total": 5, "upstream": 2, "cached": 0, "denied": 2, "queued": 0, "errors": 1}
    segments = bar_segments(row, scale=10)
    assert [(s.outcome, s.count, s.x, s.width) for s in segments] == [
        ("upstream", 2, 0.0, 20.0),
        ("denied", 2, 20.0, 20.0),
        ("errors", 1, 40.0, 10.0),
    ]


def test_bar_segments_of_an_empty_scale_are_empty() -> None:
    assert bar_segments({"total": 0}, scale=0) == []


def test_ranges_default_to_seven_days() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    assert DEFAULT_RANGE == "7d"
    assert since_for("24h", now) == now - timedelta(hours=24)
    assert since_for("unknown", now) == now - RANGES["7d"][1]
