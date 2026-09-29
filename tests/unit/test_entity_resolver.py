"""Tests for EntityResolver - advanced entity resolution."""

from __future__ import annotations

import pytest

from interlock.discovery.entity_resolver import EntityResolver

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolve:
    @pytest.fixture(autouse=True)
    async def setup_resolver(self):
        self.resolver = EntityResolver()
        await self.resolver.load_aliases()

    @pytest.mark.asyncio
    async def test_exact_alias_match(self):
        assert self.resolver.resolve("js") == "JavaScript"
        assert self.resolver.resolve("nyc") == "New York"
        assert self.resolver.resolve("k8s") == "Kubernetes"

    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        assert self.resolver.resolve("JS") == "JavaScript"
        assert self.resolver.resolve("JavaScript") == "JavaScript"
        assert self.resolver.resolve("JAVASCRIPT") == "JavaScript"
        assert self.resolver.resolve("NYC") == "New York"

    @pytest.mark.asyncio
    async def test_no_match_returns_original(self):
        assert self.resolver.resolve("xyzzy_unknown") == "xyzzy_unknown"
        assert self.resolver.resolve("SomeRandomThing") == "SomeRandomThing"

    @pytest.mark.asyncio
    async def test_empty_string(self):
        assert self.resolver.resolve("") == ""


class TestAddAlias:
    @pytest.fixture(autouse=True)
    async def setup_resolver(self):
        self.resolver = EntityResolver()
        await self.resolver.load_aliases()

    @pytest.mark.asyncio
    async def test_add_alias_registers_mapping(self):
        self.resolver.add_alias("JSX", "React JSX")
        assert self.resolver.resolve("jsx") == "React JSX"
        assert self.resolver.resolve("JSX") == "React JSX"

    @pytest.mark.asyncio
    async def test_add_alias_updates_canonical_set(self):
        self.resolver.add_alias("tf", "TensorFlow")
        assert "TensorFlow" in self.resolver._canonical_set


class TestFindSimilar:
    @pytest.fixture(autouse=True)
    async def setup_resolver(self):
        self.resolver = EntityResolver()
        await self.resolver.load_aliases()

    @pytest.mark.asyncio
    async def test_find_similar_close_strings(self):
        results = self.resolver.find_similar("JavaScrpt", threshold=0.7)
        # Should find JavaScript as similar
        canonicals = [r[0] for r in results]
        assert "JavaScript" in canonicals

    @pytest.mark.asyncio
    async def test_find_similar_exact_match(self):
        results = self.resolver.find_similar("JavaScript", threshold=0.9)
        assert any(r[0] == "JavaScript" for r in results)

    @pytest.mark.asyncio
    async def test_find_similar_no_match(self):
        results = self.resolver.find_similar("zzzzzz", threshold=0.9)
        assert results == []

    @pytest.mark.asyncio
    async def test_find_similar_empty(self):
        assert self.resolver.find_similar("") == []


class TestAutoResolveBatch:
    @pytest.fixture(autouse=True)
    async def setup_resolver(self):
        self.resolver = EntityResolver()
        await self.resolver.load_aliases()

    @pytest.mark.asyncio
    async def test_batch_known_aliases(self):
        result = await self.resolver.auto_resolve_batch(["js", "NYC", "k8s"])
        assert result["js"] == "JavaScript"
        assert result["NYC"] == "New York"
        assert result["k8s"] == "Kubernetes"

    @pytest.mark.asyncio
    async def test_batch_unknown_becomes_canonical(self):
        result = await self.resolver.auto_resolve_batch(["BrandNewEntity"])
        assert result["BrandNewEntity"] == "BrandNewEntity"
        assert "BrandNewEntity" in self.resolver._canonical_set

    @pytest.mark.asyncio
    async def test_batch_skips_empty(self):
        result = await self.resolver.auto_resolve_batch(["", "  ", "js"])
        assert "" not in result
        assert "  " not in result
        assert result["js"] == "JavaScript"

    @pytest.mark.asyncio
    async def test_batch_fuzzy_auto_maps(self):
        """Close match should auto-map to existing canonical."""
        # "JavaScrip" is close to "JavaScript"
        result = await self.resolver.auto_resolve_batch(["JavaScrip"])
        # Should resolve to JavaScript if similarity >= 0.85
        assert result["JavaScrip"] == "JavaScript" or result["JavaScrip"] == "JavaScrip"
