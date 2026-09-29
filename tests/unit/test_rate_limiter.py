"""Tests for interlock.core.rate_limiter - Redis-backed sliding window rate limiter."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.core.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_redis(script_results: list[list[int]] | None = None):
    """Create a mock Redis client with register_script support.

    script_results: list of [allowed, remaining] pairs that the Lua script
    will return on successive calls. Defaults to [[1, 9]] (allowed, 9 remaining).
    """
    if script_results is None:
        script_results = [[1, 9]]

    mock_redis = AsyncMock()

    call_index = 0

    async def script_call(keys, args):
        nonlocal call_index
        idx = min(call_index, len(script_results) - 1)
        call_index += 1
        return script_results[idx]

    mock_script = AsyncMock(side_effect=script_call)
    mock_redis.register_script = MagicMock(return_value=mock_script)

    return mock_redis, mock_script


# ---------------------------------------------------------------------------
# Tests: check()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_allows_when_under_limit():
    """Request is allowed when count is below limit."""
    mock_redis, _ = _make_mock_redis([[1, 9]])
    limiter = RateLimiter(mock_redis)
    await limiter.initialize()

    result = await limiter.check("ratelimit:user:1", limit=10, window_seconds=60)

    assert result.allowed is True
    assert result.remaining == 9
    assert result.limit == 10
    assert result.reset_at > time.time() - 1


@pytest.mark.asyncio
async def test_check_denies_when_at_limit():
    """Request is denied when count reaches limit."""
    mock_redis, _ = _make_mock_redis([[0, 0]])
    limiter = RateLimiter(mock_redis)
    await limiter.initialize()

    result = await limiter.check("ratelimit:user:1", limit=10, window_seconds=60)

    assert result.allowed is False
    assert result.remaining == 0
    assert result.limit == 10


@pytest.mark.asyncio
async def test_sliding_window_old_entries_pruned():
    """Lua script receives correct window param for pruning old entries."""
    mock_redis, mock_script = _make_mock_redis([[1, 4]])
    limiter = RateLimiter(mock_redis)
    await limiter.initialize()

    result = await limiter.check("ratelimit:user:42", limit=5, window_seconds=120)

    # Verify the script was called with the right arguments
    mock_script.assert_called_once()
    call_args = mock_script.call_args
    keys = call_args.kwargs.get("keys", call_args[0] if call_args[0] else None)
    args = call_args.kwargs.get("args", call_args[1] if len(call_args) > 1 else None)

    assert keys == ["ratelimit:user:42"]
    # args[1] = window_seconds
    assert args[1] == 120
    # args[2] = limit
    assert args[2] == 5
    assert result.allowed is True
    assert result.remaining == 4


@pytest.mark.asyncio
async def test_check_auto_initializes():
    """If initialize() wasn't called, check() calls it automatically."""
    mock_redis, _ = _make_mock_redis([[1, 5]])
    limiter = RateLimiter(mock_redis)
    # Do NOT call initialize()

    result = await limiter.check("ratelimit:user:1", limit=10, window_seconds=60)

    assert result.allowed is True
    mock_redis.register_script.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: check_multi()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_multi_returns_most_restrictive():
    """check_multi returns the dimension with fewest remaining (most restrictive)."""
    # First call (user dimension): allowed, 5 remaining
    # Second call (source dimension): allowed, 2 remaining (more restrictive)
    mock_redis, _ = _make_mock_redis([[1, 5], [1, 2]])
    limiter = RateLimiter(mock_redis)
    await limiter.initialize()

    result = await limiter.check_multi(identity_id=1, source_id="default")

    assert result.allowed is True
    assert result.remaining == 2
    assert "source" in result.dimension


@pytest.mark.asyncio
async def test_check_multi_denied_wins():
    """If any dimension is denied, the overall result is denied."""
    # First call (user dimension): allowed, 5 remaining
    # Second call (source dimension): denied
    mock_redis, _ = _make_mock_redis([[1, 5], [0, 0]])
    limiter = RateLimiter(mock_redis)
    await limiter.initialize()

    result = await limiter.check_multi(identity_id=1, source_id="default")

    assert result.allowed is False


@pytest.mark.asyncio
async def test_check_multi_user_only():
    """check_multi with only identity_id checks just the user dimension."""
    mock_redis, mock_script = _make_mock_redis([[1, 99]])
    limiter = RateLimiter(mock_redis)
    await limiter.initialize()

    result = await limiter.check_multi(identity_id=42)

    assert result.allowed is True
    # Only one call since no source_id or session_id
    assert mock_script.call_count == 1


@pytest.mark.asyncio
async def test_check_multi_custom_limits():
    """check_multi with custom limits dict overrides defaults."""
    mock_redis, _ = _make_mock_redis([[1, 3]])
    limiter = RateLimiter(mock_redis)
    await limiter.initialize()

    custom_limits = {"ratelimit:custom:test": (5, 30)}
    result = await limiter.check_multi(identity_id=1, limits=custom_limits)

    assert result.allowed is True
    assert result.remaining == 3
    assert result.dimension == "ratelimit:custom:test"


# ---------------------------------------------------------------------------
# Tests: Redis error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_error_allows_request():
    """On Redis error, rate limiter should fail open (allow the request)."""
    import redis as redis_lib

    mock_redis = AsyncMock()

    async def script_fail(keys, args):
        raise redis_lib.RedisError("Connection refused")

    mock_script = AsyncMock(side_effect=script_fail)
    mock_redis.register_script = MagicMock(return_value=mock_script)

    limiter = RateLimiter(mock_redis)
    await limiter.initialize()

    result = await limiter.check("ratelimit:user:1", limit=10, window_seconds=60)

    assert result.allowed is True
    assert result.remaining == 10
