"""Performance profiler for collecting and reporting timing metrics.

Provides a PerformanceProfiler class that records operation timings and
counters, computes statistics (avg, p50, p95, p99, min, max), and
produces summary reports. Thread-safe for use across async tasks.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager


class PerformanceProfiler:
    """Collects and reports performance metrics for InterLock components.

    Usage::

        profiler = PerformanceProfiler()

        with profiler.timer("cache_lookup"):
            result = await cache.get(key)

        profiler.increment("cache_hits")
        print(profiler.stats("cache_lookup"))
        print(profiler.report())
    """

    def __init__(self) -> None:
        self._timings: dict[str, list[float]] = defaultdict(list)
        self._counters: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    @contextmanager
    def timer(self, operation: str) -> Iterator[None]:
        """Context manager that records timing for an operation in milliseconds."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            with self._lock:
                self._timings[operation].append(elapsed_ms)

    def record(self, operation: str, duration_ms: float) -> None:
        """Manually record a timing measurement in milliseconds."""
        with self._lock:
            self._timings[operation].append(duration_ms)

    def increment(self, counter: str, amount: int = 1) -> None:
        """Increment a named counter."""
        with self._lock:
            self._counters[counter] += amount

    def stats(self, operation: str) -> dict:
        """Get stats for an operation: avg, p50, p95, p99, min, max, count.

        Returns an empty dict if no timings have been recorded for the operation.
        All timing values are in milliseconds.
        """
        with self._lock:
            timings = list(self._timings.get(operation, []))

        if not timings:
            return {}

        sorted_t = sorted(timings)
        count = len(sorted_t)

        return {
            "count": count,
            "avg_ms": statistics.mean(sorted_t),
            "p50_ms": sorted_t[int(count * 0.50)],
            "p95_ms": sorted_t[int(count * 0.95)] if count >= 20 else sorted_t[-1],
            "p99_ms": sorted_t[int(count * 0.99)] if count >= 100 else sorted_t[-1],
            "min_ms": sorted_t[0],
            "max_ms": sorted_t[-1],
        }

    def report(self) -> dict[str, dict]:
        """Get full report for all operations.

        Returns a dict mapping operation names to their stats dicts.
        """
        with self._lock:
            operations = list(self._timings.keys())
        return {op: self.stats(op) for op in operations}

    def counter_value(self, counter: str) -> int:
        """Get the current value of a named counter."""
        with self._lock:
            return self._counters.get(counter, 0)

    def counters(self) -> dict[str, int]:
        """Get all counter values."""
        with self._lock:
            return dict(self._counters)

    def reset(self) -> None:
        """Clear all collected data."""
        with self._lock:
            self._timings.clear()
            self._counters.clear()

    def format_table(self) -> str:
        """Format the full report as a human-readable table."""
        rep = self.report()
        if not rep:
            return "(no data)"

        lines = [
            f"{'Operation':<40s}  {'Count':>6s}  {'Avg':>8s}  "
            f"{'P50':>8s}  {'P95':>8s}  {'P99':>8s}  {'Min':>8s}  {'Max':>8s}",
            "-" * 120,
        ]
        for op, s in sorted(rep.items()):
            lines.append(
                f"{op:<40s}  {s['count']:>6d}  {s['avg_ms']:>7.2f}ms  "
                f"{s['p50_ms']:>7.2f}ms  {s['p95_ms']:>7.2f}ms  {s['p99_ms']:>7.2f}ms  "
                f"{s['min_ms']:>7.2f}ms  {s['max_ms']:>7.2f}ms"
            )

        counters = self.counters()
        if counters:
            lines.append("")
            lines.append("Counters:")
            for name, val in sorted(counters.items()):
                lines.append(f"  {name}: {val}")

        return "\n".join(lines)
