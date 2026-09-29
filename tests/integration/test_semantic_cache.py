"""E2E semantic cache integration tests (P3-T10).

Tests the full semantic cache pipeline without real external services.
Uses mock embeddings (small vectors) and mock LLM verifier to exercise
all confidence tiers and strategy orderings.
"""

from __future__ import annotations

import pytest

from interlock.cache.faiss_index import FAISSIndex
from interlock.cache.invalidation import CacheInvalidator
from interlock.cache.l1 import L1Cache
from interlock.cache.strategy import (
    DeterministicFirstStrategy,
    SemanticFirstStrategy,
)
from interlock.core.intent import capture_sql_intent
from interlock.core.normalizer import normalize_sql
from interlock.models import CacheResult, LLMVerifyResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIM = 4  # small dimension for testing


def _vec(a: float, b: float, c: float, d: float) -> list[float]:
    """Return a 4-dim vector."""
    return [a, b, c, d]


# Predetermined embeddings: "similar" queries get close vectors,
# "different" queries get orthogonal vectors.
EMBED_MAP: dict[str, list[float]] = {
    # Two semantically equivalent SELECT queries on the orders table
    "select_orders_a": _vec(0.9, 0.1, 0.0, 0.0),
    "select_orders_b": _vec(0.89, 0.12, 0.01, 0.0),
    # A completely different query
    "select_users": _vec(0.0, 0.0, 0.9, 0.1),
    # Medium-confidence match pair
    "medium_a": _vec(0.8, 0.2, 0.0, 0.0),
    "medium_b": _vec(0.75, 0.28, 0.05, 0.0),
    # Low-confidence pair (farther apart)
    "low_a": _vec(1.0, 0.0, 0.0, 0.0),
    "low_b": _vec(0.0, 0.0, 0.0, 1.0),
}


class MockLLMVerifier:
    """Mock LLM verifier that returns a predetermined result."""

    def __init__(self, equivalent: bool = True, confidence: float = 0.95) -> None:
        self._equivalent = equivalent
        self._confidence = confidence
        self.call_count = 0

    async def verify(self, original: str, cached: str) -> LLMVerifyResult:
        self.call_count += 1
        return LLMVerifyResult(
            equivalent=self._equivalent,
            confidence=self._confidence,
            reason="mock",
        )


class FakeL2Cache:
    """In-memory L2 cache that mimics the real L2 interface without Redis."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str) -> CacheResult:
        data = self._store.get(key)
        if data is not None:
            return CacheResult(hit=True, data=data, tier="l2")
        return CacheResult(hit=False)

    async def put(self, key: str, data: bytes, metadata: dict | None = None) -> None:
        self._store[key] = data

    async def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def faiss_index():
    idx = FAISSIndex(dimension=DIM)
    await idx.initialize()
    return idx


@pytest.fixture
def l1():
    return L1Cache(max_size=100, ttl_seconds=60)


@pytest.fixture
def fake_l2():
    return FakeL2Cache()


@pytest.fixture
def verifier_accepts():
    return MockLLMVerifier(equivalent=True)


@pytest.fixture
def verifier_rejects():
    return MockLLMVerifier(equivalent=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantically_equivalent_queries_hit_cache(faiss_index: FAISSIndex):
    """Two queries with different literals but same structure.

    First query is a cache miss and gets stored.
    Second query with a similar embedding is a semantic match.
    """
    strategy = DeterministicFirstStrategy(
        semantic_index=faiss_index,
        auto_serve_threshold=0.90,  # lower for test vectors
        verify_threshold=0.80,
    )

    emb_a = EMBED_MAP["select_orders_a"]
    emb_b = EMBED_MAP["select_orders_b"]
    data = b'[{"id": 1, "total": 100}]'

    # First lookup - miss
    result = await strategy.get(
        "key_a",
        intent_embedding=emb_a,
        intent_text="orders query A",
        semantic_filters={"source_id": "pg1"},
    )
    assert not result.hit

    # Store
    await strategy.put(
        "key_a",
        data,
        {"source_id": "pg1", "intent_text": "orders query A"},
        intent_embedding=emb_a,
        intent_text="orders query A",
    )
    await faiss_index.rebuild()

    # Second lookup with similar embedding - should hit
    result = await strategy.get(
        "key_b",
        intent_embedding=emb_b,
        intent_text="orders query B",
        semantic_filters={"source_id": "pg1"},
    )
    assert result.hit
    assert result.tier == "semantic"
    assert result.data == data


@pytest.mark.asyncio
async def test_high_confidence_auto_served(faiss_index: FAISSIndex):
    """Score >= auto_serve_threshold is served without LLM verification."""
    verifier = MockLLMVerifier(equivalent=True)
    strategy = DeterministicFirstStrategy(
        semantic_index=faiss_index,
        llm_verifier=verifier,
        auto_serve_threshold=0.90,
        verify_threshold=0.80,
    )

    emb = EMBED_MAP["select_orders_a"]
    data = b"high_conf_data"

    await strategy.put(
        "hc_key",
        data,
        {"source_id": "pg1", "intent_text": "high conf"},
        intent_embedding=emb,
        intent_text="high conf",
    )
    await faiss_index.rebuild()

    # Same embedding -> score ~1.0 -> auto-serve
    result = await strategy.get(
        "other_key",
        intent_embedding=emb,
        intent_text="high conf",
        semantic_filters={"source_id": "pg1"},
    )
    assert result.hit
    assert result.tier == "semantic"
    # LLM verifier should NOT have been called
    assert verifier.call_count == 0


@pytest.mark.asyncio
async def test_medium_confidence_triggers_llm_verify(faiss_index: FAISSIndex):
    """Score between verify and auto-serve triggers LLM verifier.
    Verifier returns equivalent=True -> cache hit.
    """
    verifier = MockLLMVerifier(equivalent=True)
    strategy = DeterministicFirstStrategy(
        semantic_index=faiss_index,
        llm_verifier=verifier,
        auto_serve_threshold=0.999,  # very high so our vectors won't reach it
        verify_threshold=0.80,
    )

    emb_a = EMBED_MAP["medium_a"]
    emb_b = EMBED_MAP["medium_b"]
    data = b"medium_conf_data"

    await strategy.put(
        "med_key",
        data,
        {"source_id": "pg1", "intent_text": "medium query A"},
        intent_embedding=emb_a,
        intent_text="medium query A",
    )
    await faiss_index.rebuild()

    result = await strategy.get(
        "other_key",
        intent_embedding=emb_b,
        intent_text="medium query B",
        semantic_filters={"source_id": "pg1"},
    )
    assert result.hit
    assert verifier.call_count == 1


@pytest.mark.asyncio
async def test_medium_confidence_llm_rejects(faiss_index: FAISSIndex):
    """Score in medium range but LLM says not equivalent -> cache miss."""
    verifier = MockLLMVerifier(equivalent=False)
    strategy = DeterministicFirstStrategy(
        semantic_index=faiss_index,
        llm_verifier=verifier,
        auto_serve_threshold=0.999,
        verify_threshold=0.80,
    )

    emb_a = EMBED_MAP["medium_a"]
    emb_b = EMBED_MAP["medium_b"]

    await strategy.put(
        "med_key",
        b"data",
        {"source_id": "pg1", "intent_text": "medium A"},
        intent_embedding=emb_a,
        intent_text="medium A",
    )
    await faiss_index.rebuild()

    result = await strategy.get(
        "other_key",
        intent_embedding=emb_b,
        intent_text="medium B",
        semantic_filters={"source_id": "pg1"},
    )
    assert not result.hit
    assert verifier.call_count == 1


@pytest.mark.asyncio
async def test_low_confidence_is_miss(faiss_index: FAISSIndex):
    """Score below verify_threshold is a miss, no LLM call."""
    verifier = MockLLMVerifier(equivalent=True)
    strategy = DeterministicFirstStrategy(
        semantic_index=faiss_index,
        llm_verifier=verifier,
        auto_serve_threshold=0.98,
        verify_threshold=0.92,
    )

    emb_a = EMBED_MAP["low_a"]
    emb_b = EMBED_MAP["low_b"]

    await strategy.put(
        "low_key",
        b"data",
        {"source_id": "pg1", "intent_text": "low A"},
        intent_embedding=emb_a,
        intent_text="low A",
    )
    await faiss_index.rebuild()

    # Orthogonal vectors -> near-zero cosine similarity -> miss
    result = await strategy.get(
        "other_key",
        intent_embedding=emb_b,
        intent_text="low B",
        semantic_filters={"source_id": "pg1"},
    )
    assert not result.hit
    assert verifier.call_count == 0


@pytest.mark.asyncio
async def test_cache_invalidation_on_write(faiss_index: FAISSIndex, l1: L1Cache):
    """Store entry, invalidate for source, verify entry is gone."""
    emb = EMBED_MAP["select_orders_a"]
    data = b"invalidation_test"

    # Put into semantic index and L1
    await faiss_index.upsert(
        "inv_key",
        emb,
        {"source_id": "pg1", "data": data, "intent_text": "inv"},
    )
    await faiss_index.rebuild()
    await l1.put("inv_key", data)

    # Verify it exists
    assert (await l1.get("inv_key")).hit
    assert (await faiss_index.count()) == 1

    # Invalidate
    invalidator = CacheInvalidator(
        l1=l1,
        semantic_index=faiss_index,
    )
    count = await invalidator.invalidate_for_tables("pg1", ["orders"])

    # After invalidation + rebuild, semantic entry should be gone
    await faiss_index.rebuild()
    assert (await faiss_index.count()) == 0
    assert not (await l1.get("inv_key")).hit


@pytest.mark.asyncio
async def test_faiss_semantic_delete_is_immediately_unsearchable(
    faiss_index: FAISSIndex,
):
    emb = EMBED_MAP["select_orders_a"]
    await faiss_index.upsert(
        "delete_key",
        emb,
        {"source_id": "pg1", "intent_text": "select orders"},
    )
    await faiss_index.rebuild()
    assert [m.key for m in await faiss_index.search(emb, top_k=5)] == ["delete_key"]

    await faiss_index.delete("delete_key")

    assert await faiss_index.search(emb, top_k=5) == []


@pytest.mark.asyncio
async def test_deterministic_first_falls_through_to_semantic(
    faiss_index: FAISSIndex,
    l1: L1Cache,
):
    """L1 miss -> L2 miss -> Semantic hit."""
    fake_l2 = FakeL2Cache()
    strategy = DeterministicFirstStrategy(
        l1=l1,
        l2=fake_l2,
        semantic_index=faiss_index,
        auto_serve_threshold=0.90,
        verify_threshold=0.80,
    )

    emb = EMBED_MAP["select_orders_a"]
    data = b"fallthrough_data"

    # Put ONLY in semantic (not in L1 or L2)
    await faiss_index.upsert(
        "ft_key",
        emb,
        {"source_id": "pg1", "data": data, "intent_text": "fallthrough"},
    )
    await faiss_index.rebuild()

    # L1 and L2 have nothing for this key
    result = await strategy.get(
        "miss_key",
        intent_embedding=emb,
        intent_text="fallthrough",
        semantic_filters={"source_id": "pg1"},
    )
    assert result.hit
    assert result.tier == "semantic"


@pytest.mark.asyncio
async def test_semantic_first_checks_semantic_before_deterministic(
    faiss_index: FAISSIndex,
    l1: L1Cache,
):
    """SemanticFirstStrategy finds semantic hit before checking L1."""
    strategy = SemanticFirstStrategy(
        l1=l1,
        semantic_index=faiss_index,
        auto_serve_threshold=0.90,
        verify_threshold=0.80,
    )

    emb = EMBED_MAP["select_orders_a"]
    semantic_data = b"semantic_data"
    l1_data = b"l1_data"

    # Put different data in semantic and L1
    await faiss_index.upsert(
        "sf_key",
        emb,
        {"source_id": "pg1", "data": semantic_data, "intent_text": "sf"},
    )
    await faiss_index.rebuild()
    await l1.put("sf_key", l1_data)

    # Semantic should be checked first and win
    result = await strategy.get(
        "sf_key",
        intent_embedding=emb,
        intent_text="sf",
        semantic_filters={"source_id": "pg1"},
    )
    assert result.hit
    assert result.tier == "semantic"
    assert result.data == semantic_data


@pytest.mark.asyncio
async def test_full_pipeline_sql_to_cache(faiss_index: FAISSIndex):
    """End-to-end: SQL -> normalize -> capture intent -> embed -> cache lookup -> store.

    Uses the real normalizer and intent capture, but fake embeddings
    derived from the intent text hash.
    """
    sql_a = "SELECT id, name, total FROM orders WHERE status = 'active' ORDER BY total"
    sql_b = "SELECT id, name, total FROM orders WHERE status = 'pending' ORDER BY total"

    # Normalize
    norm_a = normalize_sql(sql_a, source_id="pg1")
    norm_b = normalize_sql(sql_b, source_id="pg1")
    assert not norm_a.parse_error
    assert not norm_b.parse_error

    # Capture intent
    intent_a = capture_sql_intent(sql_a)
    intent_b = capture_sql_intent(sql_b)
    assert "orders" in intent_a
    assert "orders" in intent_b

    # Generate fake embeddings from intent (deterministic hash-based)
    def fake_embed(text: str) -> list[float]:
        h = hash(text) % 10000
        return [h / 10000, (h + 100) / 10000, (h + 200) / 10000, (h + 300) / 10000]

    emb_a = fake_embed(intent_a)
    # For the second query, use a very similar embedding to simulate
    # the fact that a real embedding model would produce close vectors
    # for structurally similar queries.
    emb_b = [v + 0.001 for v in emb_a]

    strategy = DeterministicFirstStrategy(
        semantic_index=faiss_index,
        auto_serve_threshold=0.90,
        verify_threshold=0.80,
    )

    data_a = b'[{"id": 1}]'

    # First query: miss, then store
    result = await strategy.get(
        norm_a.fingerprint,
        intent_embedding=emb_a,
        intent_text=intent_a,
        semantic_filters={"source_id": "pg1"},
    )
    assert not result.hit

    await strategy.put(
        norm_a.fingerprint,
        data_a,
        {"source_id": "pg1", "tables": norm_a.tables, "intent_text": intent_a},
        intent_embedding=emb_a,
        intent_text=intent_a,
    )
    await faiss_index.rebuild()

    # Second query: semantic hit (similar embedding)
    result = await strategy.get(
        norm_b.fingerprint,
        intent_embedding=emb_b,
        intent_text=intent_b,
        semantic_filters={"source_id": "pg1"},
    )
    assert result.hit
    assert result.tier == "semantic"
    assert result.data == data_a
