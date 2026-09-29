"""Advanced entity resolution - maps entity variations to canonical forms.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

# Try importing asyncpg for type annotations
try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Built-in alias defaults
# ---------------------------------------------------------------------------

_DEFAULT_ALIASES: dict[str, str] = {
    # Programming languages
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ts": "TypeScript",
    "typescript": "TypeScript",
    "py": "Python",
    "python": "Python",
    "rb": "Ruby",
    "ruby": "Ruby",
    "go": "Go",
    "golang": "Go",
    "rs": "Rust",
    "rust": "Rust",
    "cpp": "C++",
    "c++": "C++",
    "csharp": "C#",
    "c#": "C#",
    # Cities / locations
    "ny": "New York",
    "nyc": "New York",
    "new york": "New York",
    "new york city": "New York",
    "sf": "San Francisco",
    "san francisco": "San Francisco",
    "la": "Los Angeles",
    "los angeles": "Los Angeles",
    # Technologies
    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",
    "pg": "PostgreSQL",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "aws": "Amazon Web Services",
    "amazon web services": "Amazon Web Services",
    "gcp": "Google Cloud Platform",
    "google cloud": "Google Cloud Platform",
    "google cloud platform": "Google Cloud Platform",
}


class EntityResolver:
    """Resolves entity variations to canonical forms.

    Examples:
    - "JS", "JavaScript", "javascript" -> "JavaScript"
    - "NY", "New York", "NYC" -> "New York"
    - "Dr. Smith", "John Smith", "J. Smith" -> "John Smith"
    """

    def __init__(self, pg_pool: Any | None = None) -> None:
        self._pool = pg_pool
        self._alias_map: dict[str, str] = {}  # normalized alias -> canonical
        self._canonical_set: set[str] = set()

    async def load_aliases(self) -> None:
        """Load known aliases from PG (if available) or built-in defaults."""
        # Start with built-in defaults
        for alias, canonical in _DEFAULT_ALIASES.items():
            self._alias_map[alias.lower()] = canonical
            self._canonical_set.add(canonical)

        # Overlay from PG if available
        if self._pool is not None:
            try:
                rows = await self._pool.fetch("""
                    SELECT alias_text, canonical_text
                    FROM entity_aliases
                    WHERE enabled = true
                    """)
                for row in rows:
                    alias = row["alias_text"].strip().lower()
                    canonical = row["canonical_text"].strip()
                    self._alias_map[alias] = canonical
                    self._canonical_set.add(canonical)
                logger.info("Loaded %d entity aliases from database", len(rows))
            except Exception:
                logger.warning("Could not load entity aliases from database - using defaults only")

    def resolve(self, entity_text: str) -> str:
        """Resolve an entity to its canonical form.

        1. Check exact alias match (case-insensitive)
        2. Check abbreviation expansion (remove periods, e.g. "U.S.A" -> "usa")
        3. Return original if no match
        """
        if not entity_text:
            return entity_text

        normalized = entity_text.strip().lower()

        # 1. Exact alias match (case-insensitive)
        if normalized in self._alias_map:
            return self._alias_map[normalized]

        # 2. Try without periods (abbreviation expansion)
        no_periods = normalized.replace(".", "").replace(" ", "")
        if no_periods != normalized and no_periods in self._alias_map:
            return self._alias_map[no_periods]

        # 3. Check if already canonical (case-insensitive)
        for canonical in self._canonical_set:
            if canonical.lower() == normalized:
                return canonical

        return entity_text

    def add_alias(self, alias: str, canonical: str) -> None:
        """Register a new alias -> canonical mapping."""
        normalized = alias.strip().lower()
        self._alias_map[normalized] = canonical
        self._canonical_set.add(canonical)

    def find_similar(self, entity_text: str, threshold: float = 0.8) -> list[tuple[str, float]]:
        """Find similar canonical entities using string similarity.

        Uses SequenceMatcher for edit distance-based similarity.
        Returns list of (canonical, score) sorted by score descending.
        """
        if not entity_text:
            return []

        normalized = entity_text.strip().lower()
        results: list[tuple[str, float]] = []

        for canonical in self._canonical_set:
            ratio = SequenceMatcher(None, normalized, canonical.lower()).ratio()
            if ratio >= threshold:
                results.append((canonical, round(ratio, 4)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    async def auto_resolve_batch(self, entities: list[str]) -> dict[str, str]:
        """Resolve a batch of entities, learning new mappings along the way.

        For each entity:
        1. Try resolve() first
        2. If no match, check find_similar() for close matches
        3. If a similar canonical is found (>= 0.85), auto-map to it
        4. Otherwise, register the entity as a new canonical form

        Returns mapping of original entity -> resolved canonical.
        """
        result: dict[str, str] = {}

        for entity in entities:
            if not entity or not entity.strip():
                continue

            resolved = self.resolve(entity)

            # If resolve returned the original, try fuzzy matching
            if resolved == entity:
                similar = self.find_similar(entity, threshold=0.85)
                if similar:
                    # Map to the best match
                    best_canonical, _score = similar[0]
                    self.add_alias(entity, best_canonical)
                    resolved = best_canonical
                else:
                    # New canonical - register it as itself
                    canonical_form = entity.strip()
                    self._canonical_set.add(canonical_form)
                    resolved = canonical_form

            result[entity] = resolved

        return result
