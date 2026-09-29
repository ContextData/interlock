"""Adaptive cache strategies for different data source types.

Strategies implement a common Protocol interface:
- DeterministicFirstStrategy: L1 -> L2 -> Semantic (SQL sources)
- SemanticFirstStrategy: Semantic -> L1 -> L2 (file/API/SaaS sources)
- SemanticOnlyStrategy: Semantic only (search APIs)
- DeterministicOnlyStrategy: L1 -> L2 only (sources that must avoid semantic reuse)
- BypassStrategy: no cache lookup or write (sensitive/volatile sources)

Each strategy supports per-source confidence thresholds that control
whether a semantic match is auto-served, LLM-verified, or treated as
a miss.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from interlock.cache.embedding import EmbeddingEngine
from interlock.cache.faiss_index import FAISSIndex
from interlock.cache.l1 import L1Cache
from interlock.cache.l2 import L2Cache
from interlock.cache.llm_verify import LLMVerifier
from interlock.models import CacheResult, SemanticMatch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CacheStrategy(Protocol):
    """Common interface for all cache lookup strategies."""

    async def get(
        self,
        cache_key: str,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
        semantic_filters: dict | None = None,
    ) -> CacheResult: ...

    async def put(
        self,
        cache_key: str,
        data: bytes,
        metadata: dict,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
    ) -> None: ...

    async def invalidate(self, cache_key: str) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _semantic_lookup(
    semantic_index: FAISSIndex | None,
    intent_embedding: list[float] | None,
    cache_key: str,
    auto_serve_threshold: float,
    verify_threshold: float,
    llm_verifier: LLMVerifier | None,
    intent_text: str | None,
    semantic_filters: dict | None,
) -> CacheResult:
    """Perform semantic search and optionally LLM-verify medium-confidence hits.

    Returns a CacheResult with hit=True if the match passes thresholds,
    or hit=False on miss / failed verification.
    """
    if semantic_index is None or intent_embedding is None or not semantic_filters:
        return CacheResult(hit=False)

    matches: list[SemanticMatch] = await semantic_index.search(
        embedding=intent_embedding,
        top_k=1,
        filters=semantic_filters,
    )
    if not matches:
        return CacheResult(hit=False)

    best = matches[0]

    # High confidence - auto-serve
    if best.score >= auto_serve_threshold:
        return CacheResult(
            hit=True,
            data=best.metadata.get("data"),
            tier="semantic",
            confidence=best.score,
            metadata=best.metadata,
        )

    # Medium confidence - LLM verify
    if best.score >= verify_threshold:
        if llm_verifier is not None and intent_text is not None:
            cached_intent = best.metadata.get("intent_text", "")
            verify_result = await llm_verifier.verify(intent_text, cached_intent)
            if verify_result.equivalent:
                return CacheResult(
                    hit=True,
                    data=best.metadata.get("data"),
                    tier="semantic",
                    confidence=best.score,
                    metadata=best.metadata,
                )
        # Verification failed or not available - treat as miss
        return CacheResult(hit=False)

    # Below verify threshold - miss
    return CacheResult(hit=False)


async def _semantic_put(
    semantic_index: FAISSIndex | None,
    cache_key: str,
    data: bytes,
    metadata: dict,
    intent_embedding: list[float] | None,
    intent_text: str | None,
) -> None:
    """Store an entry in the semantic index if available."""
    if semantic_index is None or intent_embedding is None:
        return

    store_meta = dict(metadata)
    store_meta["data"] = data
    if intent_text is not None:
        store_meta["intent_text"] = intent_text

    await semantic_index.upsert(
        key=cache_key,
        embedding=intent_embedding,
        metadata=store_meta,
    )


async def _semantic_delete(
    semantic_index: FAISSIndex | None,
    cache_key: str,
) -> None:
    """Delete an entry from the semantic index if available."""
    if semantic_index is None:
        return
    await semantic_index.delete(cache_key)


# ---------------------------------------------------------------------------
# DeterministicFirstStrategy
# ---------------------------------------------------------------------------


class DeterministicFirstStrategy:
    """L1 -> L2 -> Semantic -> (LLM verify) -> miss.

    Best for SQL/database sources where deterministic key matches are common.
    """

    def __init__(
        self,
        l1: L1Cache | None = None,
        l2: L2Cache | None = None,
        semantic_index: FAISSIndex | None = None,
        llm_verifier: LLMVerifier | None = None,
        auto_serve_threshold: float = 0.98,
        verify_threshold: float = 0.92,
    ) -> None:
        self._l1 = l1
        self._l2 = l2
        self._semantic_index = semantic_index
        self._llm_verifier = llm_verifier
        self._auto_serve_threshold = auto_serve_threshold
        self._verify_threshold = verify_threshold

    async def get(
        self,
        cache_key: str,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
        semantic_filters: dict | None = None,
    ) -> CacheResult:
        # L1
        if self._l1 is not None:
            result = await self._l1.get(cache_key)
            if result.hit:
                return result

        # L2
        if self._l2 is not None:
            result = await self._l2.get(cache_key)
            if result.hit:
                # Promote to L1
                if self._l1 is not None and result.data is not None:
                    await self._l1.put(cache_key, result.data)
                return result

        # Semantic
        result = await _semantic_lookup(
            self._semantic_index,
            intent_embedding,
            cache_key,
            self._auto_serve_threshold,
            self._verify_threshold,
            self._llm_verifier,
            intent_text,
            semantic_filters,
        )
        if result.hit:
            return result

        return CacheResult(hit=False)

    async def put(
        self,
        cache_key: str,
        data: bytes,
        metadata: dict,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
    ) -> None:
        if self._l1 is not None:
            await self._l1.put(cache_key, data)
        if self._l2 is not None:
            await self._l2.put(cache_key, data, metadata=metadata)
        await _semantic_put(
            self._semantic_index,
            cache_key,
            data,
            metadata,
            intent_embedding,
            intent_text,
        )

    async def invalidate(self, cache_key: str) -> None:
        if self._l1 is not None:
            await self._l1.invalidate(cache_key)
        if self._l2 is not None:
            await self._l2.invalidate(cache_key)
        await _semantic_delete(self._semantic_index, cache_key)


# ---------------------------------------------------------------------------
# SemanticFirstStrategy
# ---------------------------------------------------------------------------


class SemanticFirstStrategy:
    """Semantic -> (LLM verify) -> L1 -> L2 -> miss.

    Best for file/API/SaaS sources where queries vary in phrasing.
    """

    def __init__(
        self,
        l1: L1Cache | None = None,
        l2: L2Cache | None = None,
        semantic_index: FAISSIndex | None = None,
        llm_verifier: LLMVerifier | None = None,
        auto_serve_threshold: float = 0.98,
        verify_threshold: float = 0.92,
    ) -> None:
        self._l1 = l1
        self._l2 = l2
        self._semantic_index = semantic_index
        self._llm_verifier = llm_verifier
        self._auto_serve_threshold = auto_serve_threshold
        self._verify_threshold = verify_threshold

    async def get(
        self,
        cache_key: str,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
        semantic_filters: dict | None = None,
    ) -> CacheResult:
        # Semantic first
        result = await _semantic_lookup(
            self._semantic_index,
            intent_embedding,
            cache_key,
            self._auto_serve_threshold,
            self._verify_threshold,
            self._llm_verifier,
            intent_text,
            semantic_filters,
        )
        if result.hit:
            return result

        # Fall back to L1
        if self._l1 is not None:
            result = await self._l1.get(cache_key)
            if result.hit:
                return result

        # Fall back to L2
        if self._l2 is not None:
            result = await self._l2.get(cache_key)
            if result.hit:
                return result

        return CacheResult(hit=False)

    async def put(
        self,
        cache_key: str,
        data: bytes,
        metadata: dict,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
    ) -> None:
        await _semantic_put(
            self._semantic_index,
            cache_key,
            data,
            metadata,
            intent_embedding,
            intent_text,
        )
        if self._l1 is not None:
            await self._l1.put(cache_key, data)
        if self._l2 is not None:
            await self._l2.put(cache_key, data, metadata=metadata)

    async def invalidate(self, cache_key: str) -> None:
        await _semantic_delete(self._semantic_index, cache_key)
        if self._l1 is not None:
            await self._l1.invalidate(cache_key)
        if self._l2 is not None:
            await self._l2.invalidate(cache_key)


# ---------------------------------------------------------------------------
# SemanticOnlyStrategy
# ---------------------------------------------------------------------------


class SemanticOnlyStrategy:
    """Semantic only -> (LLM verify) -> miss.

    Best for search APIs where deterministic keys are not meaningful.
    """

    def __init__(
        self,
        semantic_index: FAISSIndex | None = None,
        llm_verifier: LLMVerifier | None = None,
        auto_serve_threshold: float = 0.98,
        verify_threshold: float = 0.92,
    ) -> None:
        self._semantic_index = semantic_index
        self._llm_verifier = llm_verifier
        self._auto_serve_threshold = auto_serve_threshold
        self._verify_threshold = verify_threshold

    async def get(
        self,
        cache_key: str,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
        semantic_filters: dict | None = None,
    ) -> CacheResult:
        result = await _semantic_lookup(
            self._semantic_index,
            intent_embedding,
            cache_key,
            self._auto_serve_threshold,
            self._verify_threshold,
            self._llm_verifier,
            intent_text,
            semantic_filters,
        )
        if result.hit:
            return result
        return CacheResult(hit=False)

    async def put(
        self,
        cache_key: str,
        data: bytes,
        metadata: dict,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
    ) -> None:
        await _semantic_put(
            self._semantic_index,
            cache_key,
            data,
            metadata,
            intent_embedding,
            intent_text,
        )

    async def invalidate(self, cache_key: str) -> None:
        await _semantic_delete(self._semantic_index, cache_key)


# ---------------------------------------------------------------------------
# DeterministicOnlyStrategy
# ---------------------------------------------------------------------------


class DeterministicOnlyStrategy:
    """L1 -> L2 -> miss, with no semantic lookup or semantic writes."""

    def __init__(
        self,
        l1: L1Cache | None = None,
        l2: L2Cache | None = None,
    ) -> None:
        self._l1 = l1
        self._l2 = l2

    async def get(
        self,
        cache_key: str,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
        semantic_filters: dict | None = None,
    ) -> CacheResult:
        if self._l1 is not None:
            result = await self._l1.get(cache_key)
            if result.hit:
                return result

        if self._l2 is not None:
            result = await self._l2.get(cache_key)
            if result.hit:
                if self._l1 is not None and result.data is not None:
                    await self._l1.put(cache_key, result.data)
                return result

        return CacheResult(hit=False)

    async def put(
        self,
        cache_key: str,
        data: bytes,
        metadata: dict,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
    ) -> None:
        if self._l1 is not None:
            await self._l1.put(cache_key, data)
        if self._l2 is not None:
            await self._l2.put(cache_key, data, metadata=metadata)

    async def invalidate(self, cache_key: str) -> None:
        if self._l1 is not None:
            await self._l1.invalidate(cache_key)
        if self._l2 is not None:
            await self._l2.invalidate(cache_key)


# ---------------------------------------------------------------------------
# BypassStrategy
# ---------------------------------------------------------------------------


class BypassStrategy:
    """Explicit no-op cache strategy for sensitive or volatile sources."""

    async def get(
        self,
        cache_key: str,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
        semantic_filters: dict | None = None,
    ) -> CacheResult:
        return CacheResult(hit=False, tier="bypass")

    async def put(
        self,
        cache_key: str,
        data: bytes,
        metadata: dict,
        intent_embedding: list[float] | None = None,
        intent_text: str | None = None,
    ) -> None:
        return None

    async def invalidate(self, cache_key: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_cache_strategy(
    strategy_type: str,
    l1: L1Cache | None = None,
    l2: L2Cache | None = None,
    semantic_index: FAISSIndex | None = None,
    embedding_engine: EmbeddingEngine | None = None,
    llm_verifier: LLMVerifier | None = None,
    auto_serve_threshold: float = 0.98,
    verify_threshold: float = 0.92,
) -> CacheStrategy:
    """Create a cache strategy by type name.

    Args:
        strategy_type: One of "deterministic_first", "semantic_first",
            "semantic_only", "deterministic_only", or "bypass".
        l1: Optional L1 in-process cache.
        l2: Optional L2 Redis-backed cache.
        semantic_index: Optional FAISS semantic index.
        embedding_engine: Optional embedding engine (reserved for future use).
        llm_verifier: Optional LLM verifier for medium-confidence matches.
        auto_serve_threshold: Score >= this is auto-served (default 0.98).
        verify_threshold: Score >= this triggers LLM verify (default 0.92).

    Returns:
        A CacheStrategy instance.

    Raises:
        ValueError: If strategy_type is unknown.
    """
    if strategy_type == "deterministic_first":
        return DeterministicFirstStrategy(
            l1=l1,
            l2=l2,
            semantic_index=semantic_index,
            llm_verifier=llm_verifier,
            auto_serve_threshold=auto_serve_threshold,
            verify_threshold=verify_threshold,
        )
    elif strategy_type == "semantic_first":
        return SemanticFirstStrategy(
            l1=l1,
            l2=l2,
            semantic_index=semantic_index,
            llm_verifier=llm_verifier,
            auto_serve_threshold=auto_serve_threshold,
            verify_threshold=verify_threshold,
        )
    elif strategy_type == "semantic_only":
        return SemanticOnlyStrategy(
            semantic_index=semantic_index,
            llm_verifier=llm_verifier,
            auto_serve_threshold=auto_serve_threshold,
            verify_threshold=verify_threshold,
        )
    elif strategy_type == "deterministic_only":
        return DeterministicOnlyStrategy(l1=l1, l2=l2)
    elif strategy_type == "bypass":
        return BypassStrategy()
    else:
        raise ValueError(
            f"Unknown cache strategy type: {strategy_type!r}. "
            "Expected one of: deterministic_first, semantic_first, "
            "semantic_only, deterministic_only, bypass"
        )


class CacheStrategyResolver:
    """Return the cache strategy a given source is configured to use.

    Every source used to share one strategy object, built once at startup, so
    the `cache_strategy` column on `data_sources` was written, displayed in
    the console and editable from a select box - and never consulted. A source
    set to `bypass`, which an operator chooses when freshness or per-request
    re-evaluation matters, was cached like any other.

    Strategies are thin dispatchers over the shared L1, L2 and semantic
    components, so one instance per *strategy name* is enough and they are
    memoised. Nothing here holds cached data itself.
    """

    def __init__(
        self,
        *,
        l1: L1Cache | None = None,
        l2: L2Cache | None = None,
        semantic_index: FAISSIndex | None = None,
        embedding_engine: EmbeddingEngine | None = None,
        llm_verifier: LLMVerifier | None = None,
        default_strategy: str = "deterministic_first",
    ) -> None:
        self._components = {
            "l1": l1,
            "l2": l2,
            "semantic_index": semantic_index,
            "embedding_engine": embedding_engine,
            "llm_verifier": llm_verifier,
        }
        self._default = default_strategy
        self._instances: dict[str, CacheStrategy] = {}

    @property
    def default_strategy(self) -> str:
        return self._default

    def by_name(self, strategy_type: str | None) -> CacheStrategy:
        """Resolve one strategy by name, falling back to the default.

        An unrecognised name falls back rather than raising: a stored value
        this build does not know about must not take the gateway down, and the
        fallback is the same behaviour the source had before per-source
        strategies were honoured.
        """
        name = (strategy_type or self._default).strip().lower()
        if name not in self._instances:
            try:
                self._instances[name] = create_cache_strategy(name, **self._components)
            except ValueError:
                logger.warning("Unknown cache strategy %r; falling back to %r", name, self._default)
                self._instances[name] = create_cache_strategy(self._default, **self._components)
        return self._instances[name]

    def for_source(self, source_config: object | None) -> CacheStrategy:
        """Resolve the strategy for a registered source.

        A source the registry does not know about gets the default, which is
        what an unregistered source got before.
        """
        if source_config is None:
            return self.by_name(None)
        return self.by_name(getattr(source_config, "cache_strategy", None))
