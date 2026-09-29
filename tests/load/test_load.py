"""Load tests for Onyx components.

Simulates concurrent load against in-process components (no Docker required).
All tests are self-contained with mocks where needed.

Run with: pytest tests/load/ -v -s -m load
"""

from __future__ import annotations

import asyncio
import random

import pytest

from interlock.cache.faiss_index import FAISSIndex
from interlock.cache.l1 import L1Cache
from interlock.cache.strategy import DeterministicFirstStrategy
from interlock.connections.circuit_breaker import CircuitBreaker
from interlock.core.normalizer import normalize_sql
from interlock.core.write_classifier import WriteClassifier
from interlock.models import IdentityContext, PolicyDecision, PolicyRule
from interlock.pipeline.pii_fast import PIIFastScanner
from interlock.utils.profiler import PerformanceProfiler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Sample SQL queries for load tests
_SELECT_QUERIES = [
    "SELECT id, name FROM users WHERE age > 25 AND status = 'active'",
    "SELECT o.id, o.total FROM orders o JOIN users u ON o.user_id = u.id WHERE u.team = 'eng'",
    "SELECT COUNT(*), department FROM employees GROUP BY department HAVING COUNT(*) > 5",
    "SELECT * FROM products WHERE price BETWEEN 10 AND 100 ORDER BY price DESC LIMIT 50",
    "SELECT a.id, a.title FROM articles a WHERE a.published = true AND a.category = 'tech'",
    "SELECT u.email, p.plan_name FROM users u JOIN plans p ON u.plan_id = p.id WHERE u.active = true",
]

_WRITE_QUERIES = [
    "INSERT INTO audit_log (user_id, action) VALUES (1, 'login')",
    "UPDATE users SET status = 'inactive' WHERE last_login < '2024-01-01'",
    "DELETE FROM sessions WHERE expired_at < NOW()",
    "INSERT INTO events (type, payload) VALUES ('click', '{}')",
    "UPDATE orders SET status = 'shipped' WHERE id = 42",
    "DELETE FROM temp_data WHERE created_at < '2024-06-01'",
    "INSERT INTO notifications (user_id, message) VALUES (5, 'Hello')",
    "DROP TABLE IF EXISTS temp_staging",
    "TRUNCATE old_logs",
    "ALTER TABLE users ADD COLUMN nickname TEXT",
]

_PII_TEXTS = [
    "My SSN is 123-45-6789 and email is john@example.com",
    "Contact me at (555) 123-4567 or jane.doe@company.org",
    "Credit card: 4111-1111-1111-1111, IP: 192.168.1.100",
    "Patient MRN: MRN-1234567 needs follow-up",
    "No sensitive data in this text at all",
    "Email support@test.io and phone +1-800-555-0199",
    "SSN 987-65-4321 belongs to the applicant, card 5500 0000 0000 0004",
    "Server at 10.0.0.1 returned error for user admin@internal.net",
]


def _make_policy_rules(n: int) -> list[PolicyRule]:
    """Generate n synthetic policy rules for testing."""
    rules: list[PolicyRule] = []
    for i in range(n):
        rules.append(
            PolicyRule(
                id=i + 1,
                name=f"rule_{i}",
                priority=n - i,
                conditions={
                    "source_ids": [f"src_{i % 10}"],
                    "roles": [f"role_{i % 5}"],
                    "operation_types": ["SELECT"] if i % 3 == 0 else ["INSERT", "UPDATE"],
                },
                actions={
                    "effect": "allow" if i % 4 != 0 else "deny",
                },
                enabled=True,
            )
        )
    return rules


class _StubPolicyEngine:
    """In-memory policy engine that does not require a PG connection."""

    def __init__(self, rules: list[PolicyRule]) -> None:
        self._rules = sorted(rules, key=lambda r: r.priority, reverse=True)

    def evaluate(
        self,
        identity: IdentityContext,
        source_id: str,
        operation: str,
        tables: list[str] | None = None,
        columns: list[str] | None = None,
    ) -> PolicyDecision:
        for rule in self._rules:
            if self._matches(rule, identity, source_id, operation, tables, columns):
                effect = rule.actions.get("effect", "deny")
                return PolicyDecision(
                    allowed=(effect == "allow"),
                    rule_id=rule.id,
                    rule_name=rule.name,
                    reason=f"Matched rule '{rule.name}'",
                )
        return PolicyDecision(allowed=False, reason="No matching rule - default deny")

    @staticmethod
    def _matches(
        rule: PolicyRule,
        identity: IdentityContext,
        source_id: str,
        operation: str,
        tables: list[str] | None,
        columns: list[str] | None,
    ) -> bool:
        cond = rule.conditions
        source_ids = cond.get("source_ids")
        if source_ids and source_id not in source_ids:
            return False
        roles = cond.get("roles")
        if roles and not set(identity.roles) & set(roles):
            return False
        op_types = cond.get("operation_types")
        if op_types and operation not in op_types:
            return False
        return True


# ---------------------------------------------------------------------------
# Load tests
# ---------------------------------------------------------------------------


@pytest.mark.load
class TestLoadProfile:
    """Simulated load tests for Onyx components."""

    @pytest.mark.asyncio
    async def test_concurrent_cache_lookups(self, capsys: pytest.CaptureFixture[str]) -> None:
        """100 concurrent L1 cache lookups. Measure throughput."""
        profiler = PerformanceProfiler()
        l1 = L1Cache(max_size=10000, ttl_seconds=300)

        # Pre-populate
        for i in range(200):
            await l1.put(f"key_{i}", f"value_{i}".encode())

        async def lookup(i: int) -> None:
            with profiler.timer("l1_cache_lookup"):
                result = await l1.get(f"key_{i % 200}")
                assert result.hit
                profiler.increment("hits")

        tasks = [lookup(i) for i in range(100)]
        await asyncio.gather(*tasks)

        assert profiler.counter_value("hits") == 100

        with capsys.disabled():
            print("\n--- Concurrent Cache Lookups (100) ---")
            print(profiler.format_table())

    @pytest.mark.asyncio
    async def test_concurrent_sql_normalization(self, capsys: pytest.CaptureFixture[str]) -> None:
        """100 concurrent SQL normalizations. Measure throughput."""
        profiler = PerformanceProfiler()
        errors: list[str] = []

        async def normalize(i: int) -> None:
            sql = _SELECT_QUERIES[i % len(_SELECT_QUERIES)]
            with profiler.timer("sql_normalization"):
                result = normalize_sql(sql, source_id="load_test")
                if result.parse_error:
                    errors.append(sql)

        tasks = [normalize(i) for i in range(100)]
        await asyncio.gather(*tasks)

        assert len(errors) == 0, f"Parse errors in {len(errors)} queries"

        with capsys.disabled():
            print("\n--- Concurrent SQL Normalization (100) ---")
            print(profiler.format_table())

    @pytest.mark.asyncio
    async def test_concurrent_pii_scanning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """50 concurrent PII fast scans on varying text lengths."""
        profiler = PerformanceProfiler()
        scanner = PIIFastScanner()
        errors: list[str] = []

        async def scan(i: int) -> None:
            text = _PII_TEXTS[i % len(_PII_TEXTS)]
            # Vary text length by repeating
            multiplied = text * ((i % 5) + 1)
            with profiler.timer("pii_fast_scan"):
                try:
                    scanner.scan(multiplied)
                except Exception as exc:
                    errors.append(str(exc))

        tasks = [scan(i) for i in range(50)]
        await asyncio.gather(*tasks)

        assert len(errors) == 0, f"Scan errors: {errors}"

        with capsys.disabled():
            print("\n--- Concurrent PII Scanning (50) ---")
            print(profiler.format_table())

    @pytest.mark.asyncio
    async def test_cache_strategy_under_load(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Simulate 200 queries through DeterministicFirstStrategy.

        Mix of hits and misses. Measure hit rate and latency.
        """
        profiler = PerformanceProfiler()
        l1 = L1Cache(max_size=10000, ttl_seconds=300)
        strategy = DeterministicFirstStrategy(l1=l1)

        # Pre-populate half the keys
        for i in range(100):
            await strategy.put(f"key_{i}", f"value_{i}".encode(), metadata={"i": i})

        async def query(i: int) -> None:
            key = f"key_{i % 200}"  # half will hit, half miss
            with profiler.timer("strategy_get"):
                result = await strategy.get(cache_key=key)
                if result.hit:
                    profiler.increment("cache_hits")
                else:
                    profiler.increment("cache_misses")

        tasks = [query(i) for i in range(200)]
        await asyncio.gather(*tasks)

        hits = profiler.counter_value("cache_hits")
        misses = profiler.counter_value("cache_misses")
        assert hits + misses == 200
        assert hits > 0, "Expected some cache hits"

        with capsys.disabled():
            hit_rate = hits / (hits + misses) * 100
            print("\n--- Cache Strategy Under Load (200 queries) ---")
            print(f"  Hit rate: {hit_rate:.1f}% ({hits} hits, {misses} misses)")
            print(profiler.format_table())

    @pytest.mark.asyncio
    async def test_write_classifier_throughput(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Classify 500 SQL statements. Measure throughput."""
        profiler = PerformanceProfiler()
        classifier = WriteClassifier(critical_tables={"users", "accounts"})
        all_queries = _SELECT_QUERIES + _WRITE_QUERIES
        errors: list[str] = []

        async def classify(i: int) -> None:
            sql = all_queries[i % len(all_queries)]
            with profiler.timer("write_classify"):
                try:
                    result = classifier.classify(sql)
                    if result is not None:
                        profiler.increment("writes_detected")
                    else:
                        profiler.increment("reads_detected")
                except Exception as exc:
                    errors.append(str(exc))

        tasks = [classify(i) for i in range(500)]
        await asyncio.gather(*tasks)

        assert len(errors) == 0, f"Classification errors: {errors}"
        stats = profiler.stats("write_classify")
        assert stats["count"] == 500

        with capsys.disabled():
            writes = profiler.counter_value("writes_detected")
            reads = profiler.counter_value("reads_detected")
            print("\n--- Write Classifier Throughput (500 statements) ---")
            print(f"  Writes: {writes}, Reads: {reads}")
            print(profiler.format_table())

    @pytest.mark.asyncio
    async def test_policy_engine_evaluation_speed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Evaluate 1000 policy decisions with 50 rules. Measure latency."""
        profiler = PerformanceProfiler()
        rules = _make_policy_rules(50)
        engine = _StubPolicyEngine(rules)
        rng = random.Random(42)

        identities = [
            IdentityContext(
                identity_id=i,
                user=f"user_{i}",
                agent_type="claude_code",
                roles=[f"role_{rng.randint(0, 4)}"],
            )
            for i in range(20)
        ]

        async def evaluate(i: int) -> None:
            identity = identities[i % len(identities)]
            source_id = f"src_{rng.randint(0, 9)}"
            operation = rng.choice(["SELECT", "INSERT", "UPDATE", "DELETE"])
            with profiler.timer("policy_evaluate"):
                result = engine.evaluate(identity, source_id, operation)
                if result.allowed:
                    profiler.increment("allowed")
                else:
                    profiler.increment("denied")

        tasks = [evaluate(i) for i in range(1000)]
        await asyncio.gather(*tasks)

        stats = profiler.stats("policy_evaluate")
        assert stats["count"] == 1000

        with capsys.disabled():
            allowed = profiler.counter_value("allowed")
            denied = profiler.counter_value("denied")
            print("\n--- Policy Engine Evaluation (1000 decisions, 50 rules) ---")
            print(f"  Allowed: {allowed}, Denied: {denied}")
            print(profiler.format_table())

    @pytest.mark.asyncio
    async def test_faiss_search_at_scale(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Build index with 10K vectors, run 100 concurrent searches."""
        profiler = PerformanceProfiler()
        dim = 32
        idx = FAISSIndex(dimension=dim)
        await idx.initialize()

        rng = random.Random(42)

        # Insert 10K vectors
        for i in range(10_000):
            vec = [rng.gauss(0, 1) for _ in range(dim)]
            await idx.upsert(f"key_{i}", vec, {"i": i})
        await idx.rebuild()
        profiler.increment("vectors_indexed", 10_000)

        # Pre-generate query vectors
        query_vecs = [[rng.gauss(0, 1) for _ in range(dim)] for _ in range(100)]

        errors: list[str] = []

        async def search(i: int) -> None:
            with profiler.timer("faiss_search"):
                try:
                    results = await idx.search(query_vecs[i], top_k=5)
                    assert len(results) > 0
                except Exception as exc:
                    errors.append(str(exc))

        tasks = [search(i) for i in range(100)]
        await asyncio.gather(*tasks)

        assert len(errors) == 0, f"Search errors: {errors}"

        with capsys.disabled():
            print("\n--- FAISS Search at Scale (10K vectors, 100 queries) ---")
            print(profiler.format_table())

    @pytest.mark.asyncio
    async def test_circuit_breaker_under_load(self, capsys: pytest.CaptureFixture[str]) -> None:
        """10K rapid-fire can_execute checks. Verify no state corruption."""
        profiler = PerformanceProfiler()
        cb = CircuitBreaker(source_id="load_test")
        errors: list[str] = []

        async def check(i: int) -> None:
            with profiler.timer("circuit_breaker_check"):
                try:
                    allowed = cb.can_execute()
                    if allowed:
                        profiler.increment("allowed")
                    else:
                        profiler.increment("blocked")
                except Exception as exc:
                    errors.append(str(exc))

        tasks = [check(i) for i in range(10_000)]
        await asyncio.gather(*tasks)

        assert len(errors) == 0, f"Circuit breaker errors: {errors}"
        total = profiler.counter_value("allowed") + profiler.counter_value("blocked")
        assert total == 10_000

        with capsys.disabled():
            print("\n--- Circuit Breaker Under Load (10K checks) ---")
            print(profiler.format_table())
