"""Performance benchmarks for cache tiers and core components (P3-T11).

Measures latency of each cache tier and core operation. These are NOT
assertion-based tests - they measure and report timing data. All benchmarks
pass as long as they complete without errors.

Run separately with: pytest tests/integration/test_benchmarks.py -v -s
"""

from __future__ import annotations

import statistics
import time

import pytest

from interlock.cache.faiss_index import FAISSIndex
from interlock.cache.l1 import L1Cache
from interlock.connections.circuit_breaker import CircuitBreaker
from interlock.core.intent import capture_sql_intent
from interlock.core.normalizer import normalize_sql
from interlock.models import CacheResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

N = 1000
N_CB = 10000


def _format_stats(timings_ns: list[int], label: str) -> str:
    """Format timing stats as a table row."""
    timings_us = [t / 1000.0 for t in timings_ns]
    avg = statistics.mean(timings_us)
    p50 = statistics.median(timings_us)
    p99 = sorted(timings_us)[int(len(timings_us) * 0.99)]
    return (
        f"  {label:<30s}  "
        f"avg={avg:>8.2f}us  "
        f"p50={p50:>8.2f}us  "
        f"p99={p99:>8.2f}us  "
        f"(n={len(timings_us)})"
    )


def _format_avg(timings_ns: list[int], label: str) -> str:
    timings_us = [t / 1000.0 for t in timings_ns]
    avg = statistics.mean(timings_us)
    return f"  {label:<30s}  avg={avg:>8.2f}us  (n={len(timings_us)})"


class FakeL2:
    """Minimal in-memory L2 mock for benchmarking."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str) -> CacheResult:
        data = self._store.get(key)
        if data is not None:
            return CacheResult(hit=True, data=data, tier="l2")
        return CacheResult(hit=False)

    async def put(self, key: str, data: bytes, metadata: dict | None = None) -> None:
        self._store[key] = data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_bench_l1_cache_hit(capsys):
    """Benchmark L1 (in-process TTLCache) hit latency over N lookups."""
    l1 = L1Cache(max_size=10000, ttl_seconds=300)

    # Pre-populate
    for i in range(N):
        await l1.put(f"key_{i}", f"value_{i}".encode())

    timings: list[int] = []
    for i in range(N):
        start = time.perf_counter_ns()
        result = await l1.get(f"key_{i}")
        elapsed = time.perf_counter_ns() - start
        assert result.hit
        timings.append(elapsed)

    with capsys.disabled():
        print()
        print(_format_stats(timings, "L1 cache hit"))


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_bench_l2_cache_hit(capsys):
    """Benchmark L2 (mock in-memory) hit latency over N lookups."""
    l2 = FakeL2()

    for i in range(N):
        await l2.put(f"key_{i}", f"value_{i}".encode())

    timings: list[int] = []
    for i in range(N):
        start = time.perf_counter_ns()
        result = await l2.get(f"key_{i}")
        elapsed = time.perf_counter_ns() - start
        assert result.hit
        timings.append(elapsed)

    with capsys.disabled():
        print()
        print(_format_stats(timings, "L2 cache hit (mock)"))


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_bench_sql_normalization(capsys):
    """Benchmark SQL normalization over N iterations."""
    queries = [
        "SELECT id, name FROM users WHERE age > 25 AND status = 'active'",
        "SELECT o.id, o.total FROM orders o JOIN users u ON o.user_id = u.id WHERE u.team = 'eng'",
        "SELECT COUNT(*), department FROM employees GROUP BY department HAVING COUNT(*) > 5",
        "SELECT * FROM products WHERE price BETWEEN 10 AND 100 ORDER BY price DESC LIMIT 50",
        "INSERT INTO audit_log (user_id, action) VALUES (1, 'login')",
    ]

    timings: list[int] = []
    for i in range(N):
        sql = queries[i % len(queries)]
        start = time.perf_counter_ns()
        result = normalize_sql(sql, source_id="bench")
        elapsed = time.perf_counter_ns() - start
        assert not result.parse_error
        timings.append(elapsed)

    with capsys.disabled():
        print()
        print(_format_stats(timings, "SQL normalization"))


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_bench_faiss_search(capsys):
    """Benchmark FAISS search on an index with N vectors."""
    dim = 32  # small but realistic-ish dimension
    idx = FAISSIndex(dimension=dim)
    await idx.initialize()

    # Insert N vectors
    import random

    rng = random.Random(42)
    for i in range(N):
        vec = [rng.gauss(0, 1) for _ in range(dim)]
        await idx.upsert(f"key_{i}", vec, {"i": i})
    await idx.rebuild()

    # Generate query vectors
    query_vecs = [[rng.gauss(0, 1) for _ in range(dim)] for _ in range(N)]

    timings: list[int] = []
    for i in range(N):
        start = time.perf_counter_ns()
        results = await idx.search(query_vecs[i], top_k=5)
        elapsed = time.perf_counter_ns() - start
        timings.append(elapsed)

    with capsys.disabled():
        print()
        print(_format_stats(timings, f"FAISS search ({N} vectors)"))


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_bench_intent_capture(capsys):
    """Benchmark SQL intent capture over N iterations."""
    queries = [
        "SELECT id, name FROM users WHERE age > 25",
        "SELECT o.id, o.total FROM orders o JOIN users u ON o.user_id = u.id",
        "SELECT COUNT(*) FROM employees GROUP BY department",
        "UPDATE users SET status = 'inactive' WHERE last_login < '2024-01-01'",
        "DELETE FROM sessions WHERE expired_at < NOW()",
    ]

    timings: list[int] = []
    for i in range(N):
        sql = queries[i % len(queries)]
        start = time.perf_counter_ns()
        intent = capture_sql_intent(sql)
        elapsed = time.perf_counter_ns() - start
        assert intent  # non-empty
        timings.append(elapsed)

    with capsys.disabled():
        print()
        print(_format_stats(timings, "Intent capture"))


@pytest.mark.benchmark
def test_bench_circuit_breaker(capsys):
    """Benchmark circuit breaker can_execute check over N_CB iterations."""
    cb = CircuitBreaker(source_id="bench")

    timings: list[int] = []
    for _ in range(N_CB):
        start = time.perf_counter_ns()
        cb.can_execute()
        elapsed = time.perf_counter_ns() - start
        timings.append(elapsed)

    with capsys.disabled():
        print()
        print(_format_avg(timings, "Circuit breaker can_execute"))
        print()
        print("--- Benchmark summary complete ---")
