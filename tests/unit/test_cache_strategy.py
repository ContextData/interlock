"""Unit tests for adaptive cache strategies (P3-T05)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from interlock.cache.strategy import (
    DeterministicFirstStrategy,
    SemanticFirstStrategy,
    SemanticOnlyStrategy,
    create_cache_strategy,
)
from interlock.models import CacheResult, LLMVerifyResult, SemanticMatch

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_l1() -> AsyncMock:
    l1 = AsyncMock()
    l1.get = AsyncMock(return_value=CacheResult(hit=False))
    l1.put = AsyncMock()
    l1.invalidate = AsyncMock()
    l1.clear = AsyncMock()
    return l1


@pytest.fixture()
def mock_l2() -> AsyncMock:
    l2 = AsyncMock()
    l2.get = AsyncMock(return_value=CacheResult(hit=False))
    l2.put = AsyncMock()
    l2.invalidate = AsyncMock()
    return l2


@pytest.fixture()
def mock_semantic() -> AsyncMock:
    si = AsyncMock()
    si.search = AsyncMock(return_value=[])
    si.upsert = AsyncMock()
    si.delete = AsyncMock()
    return si


@pytest.fixture()
def mock_verifier() -> AsyncMock:
    v = AsyncMock()
    v.verify = AsyncMock(
        return_value=LLMVerifyResult(equivalent=True, confidence=0.95, reason="match")
    )
    return v


# =========================================================================
# DeterministicFirstStrategy
# =========================================================================


class TestDeterministicFirstStrategy:
    """Tests for L1 -> L2 -> Semantic -> LLM verify -> miss."""

    @pytest.mark.asyncio
    async def test_l1_hit_returns_immediately(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        mock_l1.get.return_value = CacheResult(hit=True, data=b"l1data", tier="l1")
        strategy = DeterministicFirstStrategy(l1=mock_l1, l2=mock_l2, semantic_index=mock_semantic)

        result = await strategy.get("key1")

        assert result.hit is True
        assert result.data == b"l1data"
        assert result.tier == "l1"
        mock_l2.get.assert_not_awaited()
        mock_semantic.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_l1_miss_l2_hit_with_promotion(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock
    ) -> None:
        mock_l2.get.return_value = CacheResult(
            hit=True, data=b"l2data", tier="l2", metadata={"x": 1}
        )
        strategy = DeterministicFirstStrategy(l1=mock_l1, l2=mock_l2)

        result = await strategy.get("key1")

        assert result.hit is True
        assert result.tier == "l2"
        # Verify L1 promotion
        mock_l1.put.assert_awaited_once_with("key1", b"l2data")

    @pytest.mark.asyncio
    async def test_l1_l2_miss_semantic_high_confidence(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        """Semantic match with score >= auto_serve_threshold is served directly."""
        mock_semantic.search.return_value = [
            SemanticMatch(
                key="cached_key",
                score=0.99,
                metadata={"data": b"sem_data", "intent_text": "select from users"},
            )
        ]
        strategy = DeterministicFirstStrategy(
            l1=mock_l1,
            l2=mock_l2,
            semantic_index=mock_semantic,
            auto_serve_threshold=0.98,
        )

        result = await strategy.get(
            "key1",
            intent_embedding=[0.1] * 384,
            semantic_filters={"source_id": "src-1", "identity_scope": "team-a"},
        )

        assert result.hit is True
        assert result.tier == "semantic"
        assert result.confidence == 0.99
        mock_semantic.search.assert_awaited_once_with(
            embedding=[0.1] * 384,
            top_k=1,
            filters={"source_id": "src-1", "identity_scope": "team-a"},
        )

    @pytest.mark.asyncio
    async def test_semantic_medium_confidence_llm_verify(
        self,
        mock_l1: AsyncMock,
        mock_l2: AsyncMock,
        mock_semantic: AsyncMock,
        mock_verifier: AsyncMock,
    ) -> None:
        """Score between verify and auto_serve triggers LLM verification."""
        mock_semantic.search.return_value = [
            SemanticMatch(
                key="cached_key",
                score=0.95,
                metadata={"data": b"sem_data", "intent_text": "select users"},
            )
        ]
        strategy = DeterministicFirstStrategy(
            l1=mock_l1,
            l2=mock_l2,
            semantic_index=mock_semantic,
            llm_verifier=mock_verifier,
            auto_serve_threshold=0.98,
            verify_threshold=0.92,
        )

        result = await strategy.get(
            "key1",
            intent_embedding=[0.1] * 384,
            intent_text="select from users",
            semantic_filters={"source_id": "src-1"},
        )

        assert result.hit is True
        assert result.tier == "semantic"
        mock_verifier.verify.assert_awaited_once_with("select from users", "select users")

    @pytest.mark.asyncio
    async def test_semantic_medium_confidence_llm_rejects(
        self,
        mock_l1: AsyncMock,
        mock_l2: AsyncMock,
        mock_semantic: AsyncMock,
        mock_verifier: AsyncMock,
    ) -> None:
        """LLM says not equivalent - should be a miss."""
        mock_semantic.search.return_value = [
            SemanticMatch(
                key="cached_key",
                score=0.94,
                metadata={"data": b"sem_data", "intent_text": "select orders"},
            )
        ]
        mock_verifier.verify.return_value = LLMVerifyResult(
            equivalent=False, confidence=0.3, reason="different tables"
        )
        strategy = DeterministicFirstStrategy(
            l1=mock_l1,
            l2=mock_l2,
            semantic_index=mock_semantic,
            llm_verifier=mock_verifier,
            auto_serve_threshold=0.98,
            verify_threshold=0.92,
        )

        result = await strategy.get(
            "key1",
            intent_embedding=[0.1] * 384,
            intent_text="select users",
            semantic_filters={"source_id": "src-1"},
        )

        assert result.hit is False

    @pytest.mark.asyncio
    async def test_full_miss(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        """All tiers miss."""
        strategy = DeterministicFirstStrategy(l1=mock_l1, l2=mock_l2, semantic_index=mock_semantic)
        result = await strategy.get("key1", intent_embedding=[0.1] * 384)
        assert result.hit is False
        mock_semantic.search.assert_not_awaited()


# =========================================================================
# SemanticFirstStrategy
# =========================================================================


class TestSemanticFirstStrategy:
    """Tests for Semantic -> L1 -> L2 -> miss."""

    @pytest.mark.asyncio
    async def test_semantic_hit_first(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        mock_semantic.search.return_value = [
            SemanticMatch(
                key="cached",
                score=0.99,
                metadata={"data": b"sem_data"},
            )
        ]
        strategy = SemanticFirstStrategy(l1=mock_l1, l2=mock_l2, semantic_index=mock_semantic)

        result = await strategy.get(
            "key1",
            intent_embedding=[0.1] * 384,
            semantic_filters={"source_id": "src-1"},
        )

        assert result.hit is True
        assert result.tier == "semantic"
        # L1 and L2 should NOT be checked
        mock_l1.get.assert_not_awaited()
        mock_l2.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_semantic_miss_falls_to_l1(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        mock_l1.get.return_value = CacheResult(hit=True, data=b"l1", tier="l1")
        strategy = SemanticFirstStrategy(l1=mock_l1, l2=mock_l2, semantic_index=mock_semantic)

        result = await strategy.get(
            "key1",
            intent_embedding=[0.1] * 384,
            semantic_filters={"source_id": "src-1"},
        )

        assert result.hit is True
        assert result.tier == "l1"


# =========================================================================
# SemanticOnlyStrategy
# =========================================================================


class TestSemanticOnlyStrategy:
    """Tests for Semantic only -> miss."""

    @pytest.mark.asyncio
    async def test_only_uses_semantic(self, mock_semantic: AsyncMock) -> None:
        mock_semantic.search.return_value = [
            SemanticMatch(
                key="cached",
                score=0.99,
                metadata={"data": b"data"},
            )
        ]
        strategy = SemanticOnlyStrategy(semantic_index=mock_semantic)

        result = await strategy.get(
            "key1",
            intent_embedding=[0.1] * 384,
            semantic_filters={"source_id": "src-1"},
        )

        assert result.hit is True
        assert result.tier == "semantic"

    @pytest.mark.asyncio
    async def test_miss_without_embedding(self, mock_semantic: AsyncMock) -> None:
        strategy = SemanticOnlyStrategy(semantic_index=mock_semantic)
        result = await strategy.get("key1")
        assert result.hit is False
        mock_semantic.search.assert_not_awaited()


# =========================================================================
# Put & Invalidate
# =========================================================================


class TestPutAndInvalidate:
    """Test that put/invalidate operate on all configured tiers."""

    @pytest.mark.asyncio
    async def test_deterministic_put_stores_all_tiers(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        strategy = DeterministicFirstStrategy(l1=mock_l1, l2=mock_l2, semantic_index=mock_semantic)
        await strategy.put(
            "key1",
            b"data",
            {"source": "test"},
            intent_embedding=[0.1] * 384,
            intent_text="select users",
        )

        mock_l1.put.assert_awaited_once_with("key1", b"data")
        mock_l2.put.assert_awaited_once_with("key1", b"data", metadata={"source": "test"})
        mock_semantic.upsert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_semantic_only_put_skips_l1_l2(self, mock_semantic: AsyncMock) -> None:
        strategy = SemanticOnlyStrategy(semantic_index=mock_semantic)
        await strategy.put(
            "key1",
            b"data",
            {"source": "test"},
            intent_embedding=[0.1] * 384,
        )

        mock_semantic.upsert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalidate_removes_from_all_tiers(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        strategy = DeterministicFirstStrategy(l1=mock_l1, l2=mock_l2, semantic_index=mock_semantic)
        await strategy.invalidate("key1")

        mock_l1.invalidate.assert_awaited_once_with("key1")
        mock_l2.invalidate.assert_awaited_once_with("key1")
        mock_semantic.delete.assert_awaited_once_with("key1")


# =========================================================================
# Factory
# =========================================================================


class TestFactory:
    """Test the create_cache_strategy factory function."""

    def test_creates_deterministic_first(self) -> None:
        strategy = create_cache_strategy("deterministic_first")
        assert isinstance(strategy, DeterministicFirstStrategy)

    def test_creates_semantic_first(self) -> None:
        strategy = create_cache_strategy("semantic_first")
        assert isinstance(strategy, SemanticFirstStrategy)

    def test_creates_semantic_only(self) -> None:
        strategy = create_cache_strategy("semantic_only")
        assert isinstance(strategy, SemanticOnlyStrategy)

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown cache strategy type"):
            create_cache_strategy("bogus")

    def test_passes_thresholds(self) -> None:
        strategy = create_cache_strategy(
            "deterministic_first",
            auto_serve_threshold=0.99,
            verify_threshold=0.90,
        )
        assert isinstance(strategy, DeterministicFirstStrategy)
        assert strategy._auto_serve_threshold == 0.99
        assert strategy._verify_threshold == 0.90


# =========================================================================
# Per-source thresholds
# =========================================================================


class TestPerSourceThresholds:
    """Verify that per-source thresholds are applied correctly."""

    @pytest.mark.asyncio
    async def test_custom_auto_serve_threshold(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        """Score 0.96 is below default 0.98 but above custom 0.95."""
        mock_semantic.search.return_value = [
            SemanticMatch(
                key="cached",
                score=0.96,
                metadata={"data": b"data", "intent_text": "test"},
            )
        ]
        strategy = DeterministicFirstStrategy(
            l1=mock_l1,
            l2=mock_l2,
            semantic_index=mock_semantic,
            auto_serve_threshold=0.95,
            verify_threshold=0.90,
        )

        result = await strategy.get(
            "key1",
            intent_embedding=[0.1] * 384,
            semantic_filters={"source_id": "src-1"},
        )

        assert result.hit is True
        assert result.tier == "semantic"

    @pytest.mark.asyncio
    async def test_custom_verify_threshold_miss(
        self, mock_l1: AsyncMock, mock_l2: AsyncMock, mock_semantic: AsyncMock
    ) -> None:
        """Score 0.91 is below custom verify threshold of 0.95 - should be miss."""
        mock_semantic.search.return_value = [
            SemanticMatch(
                key="cached",
                score=0.91,
                metadata={"data": b"data"},
            )
        ]
        strategy = DeterministicFirstStrategy(
            l1=mock_l1,
            l2=mock_l2,
            semantic_index=mock_semantic,
            auto_serve_threshold=0.98,
            verify_threshold=0.95,
        )

        result = await strategy.get("key1", intent_embedding=[0.1] * 384)

        assert result.hit is False
