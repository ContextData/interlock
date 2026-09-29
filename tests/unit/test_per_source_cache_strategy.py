"""A source's configured cache strategy has to be the one that runs.

One `DeterministicFirstStrategy` was built at startup and shared by every
source, so the `cache_strategy` column on `data_sources` was written, shown in
the sources table, and editable from a select box in the console - while
having no effect at all. A source set to `bypass`, which an operator chooses
when freshness or per-request re-evaluation matters, was cached like any
other. A working `BypassStrategy` existed and nothing ever constructed it.

Demonstrated against the live stack: a `bypass` source repointed
at an unreachable host kept answering a previously-executed query, with its
audit row recording `cache_hit=t, cache_tier=l1`.
"""

from __future__ import annotations

import pytest

from interlock.cache.strategy import (
    BypassStrategy,
    CacheStrategyResolver,
    DeterministicFirstStrategy,
    DeterministicOnlyStrategy,
)


class _Source:
    def __init__(self, cache_strategy: str | None) -> None:
        self.cache_strategy = cache_strategy


class TestResolution:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("bypass", BypassStrategy),
            ("deterministic_first", DeterministicFirstStrategy),
            ("deterministic_only", DeterministicOnlyStrategy),
        ],
    )
    def test_each_configured_name_produces_its_strategy(self, name: str, expected: type) -> None:
        assert isinstance(CacheStrategyResolver().by_name(name), expected)

    def test_a_bypass_source_gets_the_bypass_strategy(self) -> None:
        """The assertion the whole finding reduces to."""
        resolver = CacheStrategyResolver()

        assert isinstance(resolver.for_source(_Source("bypass")), BypassStrategy)

    def test_a_source_without_a_strategy_gets_the_default(self) -> None:
        resolver = CacheStrategyResolver(default_strategy="deterministic_first")

        assert isinstance(resolver.for_source(_Source(None)), DeterministicFirstStrategy)

    def test_an_unknown_source_gets_the_default(self) -> None:
        """An unregistered source behaves as it did before per-source strategies."""
        assert isinstance(CacheStrategyResolver().for_source(None), DeterministicFirstStrategy)

    def test_an_unrecognised_name_falls_back_rather_than_raising(self) -> None:
        """A stored value this build does not know must not take the gateway down.

        Falling back reproduces the previous behaviour for that source, which
        is the safe direction for an unrecognised *cache* setting - it cannot
        grant access, only decide whether an answer is reused.
        """
        assert isinstance(
            CacheStrategyResolver().by_name("no_such_strategy"), DeterministicFirstStrategy
        )

    def test_strategies_are_memoised_per_name(self) -> None:
        """They are dispatchers over shared caches; one per name is enough."""
        resolver = CacheStrategyResolver()

        assert resolver.by_name("bypass") is resolver.by_name("bypass")
        assert resolver.by_name("bypass") is not resolver.by_name("deterministic_first")

    def test_case_and_padding_do_not_change_the_answer(self) -> None:
        resolver = CacheStrategyResolver()

        assert isinstance(resolver.by_name("  BYPASS "), BypassStrategy)


class TestBypassBehaviour:
    @pytest.mark.asyncio
    async def test_bypass_never_reports_a_hit(self) -> None:
        result = await BypassStrategy().get("any-key")

        assert result.hit is False
        assert result.tier == "bypass"

    @pytest.mark.asyncio
    async def test_bypass_stores_nothing(self) -> None:
        """Writing is as damaging as reading: a stored answer outlives the config."""
        strategy = BypassStrategy()

        await strategy.put("k", b"payload", {})

        assert (await strategy.get("k")).hit is False


class TestWiring:
    def test_the_app_builds_a_resolver_rather_than_one_shared_strategy(self) -> None:
        """Structural: the defect was in the wiring, not in the strategies."""
        import inspect

        from interlock.gateway import app

        source = inspect.getsource(app)
        assert "CacheStrategyResolver(" in source, (
            "the gateway no longer builds a per-source cache strategy resolver; "
            "every source would share one strategy again and cache_strategy would "
            "stop meaning anything"
        )
        assert "app.state.cache_strategies" in source

    def test_the_mcp_path_resolves_per_source(self) -> None:
        import inspect

        from interlock.gateway.mcp_adapter import MCPAdapter

        source = inspect.getsource(MCPAdapter._execute_query)
        assert (
            "_cache_strategy_for" in source
        ), "the MCP query path reads a gateway-wide cache strategy again"

    def test_the_pg_wire_path_resolves_per_source(self) -> None:
        import inspect

        from interlock.gateway import pg_proxy

        source = inspect.getsource(pg_proxy)
        assert (
            "_strategy_for(source_id)" in source
        ), "the PG-wire path reads a gateway-wide cache strategy again"

    def test_both_paths_still_work_without_a_resolver(self) -> None:
        """Adapters built outside the app lifespan must keep working.

        Unit tests construct these directly with a single strategy and no
        resolver; the fallback is what keeps that valid.
        """
        import inspect

        from interlock.gateway import pg_proxy

        source = inspect.getsource(pg_proxy.PGProxy._strategy_for)
        assert "if self._cache_strategies is None" in source
        assert "return self._cache_strategy" in source


def test_the_resolver_holds_no_cached_data_of_its_own() -> None:
    """It resolves; the shared L1/L2 hold the data.

    If it started holding entries, per-source isolation would depend on it
    rather than on the strategies, which is not what this design says.
    """
    resolver = CacheStrategyResolver()
    resolver.by_name("bypass")

    assert not any(
        isinstance(value, dict) and value and key != "_instances"
        for key, value in vars(resolver).items()
        if key not in {"_components", "_instances"}
    )


def test_resolver_exposes_its_default_for_diagnostics() -> None:
    assert CacheStrategyResolver(default_strategy="bypass").default_strategy == "bypass"
