"""Latency budget verification tests.

Verify the architecture's latency targets using hard assertions for the local
component budgets covered by this suite.

Run with: pytest tests/load/test_latency_budgets.py -v -s -m load
"""

from __future__ import annotations

import pytest

from interlock.cache.l1 import L1Cache
from interlock.core.intent import capture_sql_intent
from interlock.core.normalizer import normalize_sql
from interlock.models import CacheResult
from interlock.pipeline.pii_fast import PIIFastScanner
from interlock.utils.profiler import PerformanceProfiler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

N_ITERATIONS = 500


class _FakeL2:
    """Minimal in-memory L2 mock for latency testing."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str) -> CacheResult:
        data = self._store.get(key)
        if data is not None:
            return CacheResult(hit=True, data=data, tier="l2")
        return CacheResult(hit=False)

    async def put(self, key: str, data: bytes, metadata: dict | None = None) -> None:
        self._store[key] = data


def _check_budget(
    profiler: PerformanceProfiler,
    operation: str,
    budget_ms: float,
    percentile: str = "p99_ms",
) -> None:
    """Check if the measured latency is within budget."""
    stats = profiler.stats(operation)
    assert stats, f"No timing data for '{operation}'"

    actual = stats[percentile]
    assert actual <= budget_ms, (
        f"LATENCY BUDGET EXCEEDED: {operation} {percentile}={actual:.3f}ms "
        f"(budget: {budget_ms}ms)"
    )


# ---------------------------------------------------------------------------
# Latency budget tests
# ---------------------------------------------------------------------------


@pytest.mark.load
class TestLatencyBudgets:
    """Verify latency budgets from architecture spec."""

    @pytest.mark.asyncio
    async def test_l1_cache_under_5ms(self, capsys: pytest.CaptureFixture[str]) -> None:
        """L1 cache hit should be < 5ms p99."""
        profiler = PerformanceProfiler()
        l1 = L1Cache(max_size=10000, ttl_seconds=300)

        # Pre-populate
        for i in range(N_ITERATIONS):
            await l1.put(f"key_{i}", f"value_{i}".encode())

        for i in range(N_ITERATIONS):
            with profiler.timer("l1_cache_hit"):
                result = await l1.get(f"key_{i}")
                assert result.hit

        _check_budget(profiler, "l1_cache_hit", budget_ms=5.0)

        with capsys.disabled():
            stats = profiler.stats("l1_cache_hit")
            print(f"\n  L1 cache hit: p99={stats['p99_ms']:.3f}ms (budget: 5ms)")

    @pytest.mark.asyncio
    async def test_l2_cache_under_10ms(self, capsys: pytest.CaptureFixture[str]) -> None:
        """L2 cache hit should be < 10ms p99 (mock Redis)."""
        profiler = PerformanceProfiler()
        l2 = _FakeL2()

        # Pre-populate
        for i in range(N_ITERATIONS):
            await l2.put(f"key_{i}", f"value_{i}".encode())

        for i in range(N_ITERATIONS):
            with profiler.timer("l2_cache_hit"):
                result = await l2.get(f"key_{i}")
                assert result.hit

        _check_budget(profiler, "l2_cache_hit", budget_ms=10.0)

        with capsys.disabled():
            stats = profiler.stats("l2_cache_hit")
            print(f"\n  L2 cache hit (mock): p99={stats['p99_ms']:.3f}ms (budget: 10ms)")

    @pytest.mark.asyncio
    async def test_sql_normalization_under_5ms(self, capsys: pytest.CaptureFixture[str]) -> None:
        """SQL normalization should be < 5ms p99 for typical queries."""
        profiler = PerformanceProfiler()
        queries = [
            "SELECT id, name FROM users WHERE age > 25 AND status = 'active'",
            "SELECT o.id, o.total FROM orders o JOIN users u ON o.user_id = u.id WHERE u.team = 'eng'",
            "SELECT COUNT(*), department FROM employees GROUP BY department HAVING COUNT(*) > 5",
            "SELECT * FROM products WHERE price BETWEEN 10 AND 100 ORDER BY price DESC LIMIT 50",
            "INSERT INTO audit_log (user_id, action) VALUES (1, 'login')",
        ]

        for i in range(N_ITERATIONS):
            sql = queries[i % len(queries)]
            with profiler.timer("sql_normalization"):
                result = normalize_sql(sql, source_id="budget_test")
                assert not result.parse_error

        _check_budget(profiler, "sql_normalization", budget_ms=5.0)

        with capsys.disabled():
            stats = profiler.stats("sql_normalization")
            print(f"\n  SQL normalization: p99={stats['p99_ms']:.3f}ms (budget: 5ms)")

    @pytest.mark.asyncio
    async def test_pii_fast_scan_under_1ms(self, capsys: pytest.CaptureFixture[str]) -> None:
        """PII fast tier should be < 1ms per field p99."""
        profiler = PerformanceProfiler()
        scanner = PIIFastScanner()

        # Short realistic field values
        fields = [
            "John Smith",
            "john@example.com",
            "123-45-6789",
            "(555) 123-4567",
            "4111-1111-1111-1111",
            "192.168.1.1",
            "Some normal text without PII",
            "Another field value here",
            "42 Main Street, Springfield",
            "2024-01-15",
        ]

        for i in range(N_ITERATIONS):
            field = fields[i % len(fields)]
            with profiler.timer("pii_fast_scan"):
                scanner.scan(field)

        _check_budget(profiler, "pii_fast_scan", budget_ms=1.0)

        with capsys.disabled():
            stats = profiler.stats("pii_fast_scan")
            print(f"\n  PII fast scan: p99={stats['p99_ms']:.3f}ms (budget: 1ms)")

    @pytest.mark.asyncio
    async def test_intent_capture_under_2ms(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Intent capture should be < 2ms p99."""
        profiler = PerformanceProfiler()
        queries = [
            "SELECT id, name FROM users WHERE age > 25",
            "SELECT o.id, o.total FROM orders o JOIN users u ON o.user_id = u.id",
            "SELECT COUNT(*) FROM employees GROUP BY department",
            "UPDATE users SET status = 'inactive' WHERE last_login < '2024-01-01'",
            "DELETE FROM sessions WHERE expired_at < NOW()",
        ]

        for i in range(N_ITERATIONS):
            sql = queries[i % len(queries)]
            with profiler.timer("intent_capture"):
                intent = capture_sql_intent(sql)
                assert intent  # non-empty

        _check_budget(profiler, "intent_capture", budget_ms=2.0)

        with capsys.disabled():
            stats = profiler.stats("intent_capture")
            print(f"\n  Intent capture: p99={stats['p99_ms']:.3f}ms (budget: 2ms)")
