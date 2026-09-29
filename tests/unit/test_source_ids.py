"""Source ids are generated from the display name, never typed by the operator."""

from __future__ import annotations

from typing import Any

import pytest

from interlock.admin.source_ids import (
    SOURCE_ID_RE,
    InvalidSourceIdError,
    generate_source_id,
    slugify_source_id,
    validate_source_id,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Sales PostgreSQL", "sales_postgresql"),
        ("  HR / MySQL (prod)  ", "hr_mysql_prod"),
        ("Café Zürich", "cafe_zurich"),
        ("docs_s3", "docs_s3"),
        ("2026 Ledger", "2026_ledger"),
        ("!!!", "source"),
        ("", "source"),
        ("日本語", "source"),
    ],
)
def test_slug(name: str, expected: str) -> None:
    assert slugify_source_id(name) == expected


def test_slug_is_capped_and_always_a_valid_source_id() -> None:
    slug = slugify_source_id("A very long display name " * 10)
    assert len(slug) <= 48
    assert not slug.endswith("_")
    assert SOURCE_ID_RE.fullmatch(slug)


@pytest.mark.parametrize("value", ["sales_pg", "a", "x-1", "0abc"])
def test_valid_ids(value: str) -> None:
    assert validate_source_id(value) == value


@pytest.mark.parametrize("value", ["", "Sales", "_x", "-x", "a b", "a/b", "x" * 64, "ä"])
def test_invalid_ids(value: str) -> None:
    with pytest.raises(InvalidSourceIdError):
        validate_source_id(value)


class _Conn:
    def __init__(self, taken: set[str]) -> None:
        self.taken = taken
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, str]]:
        self.queries.append((query, args))
        base, like = args
        prefix = like.replace("\\_", "_").rstrip("%")
        return [{"source_id": t} for t in self.taken if t == base or t.startswith(prefix)]


@pytest.mark.anyio
async def test_generate_uses_the_slug_when_free() -> None:
    assert await generate_source_id(_Conn(set()), "Sales PostgreSQL") == "sales_postgresql"


@pytest.mark.anyio
async def test_generate_suffixes_on_collision() -> None:
    conn = _Conn({"sales_pg", "sales_pg_2", "sales_pg_3"})
    assert await generate_source_id(conn, "Sales PG") == "sales_pg_4"


@pytest.mark.anyio
async def test_generate_escapes_like_wildcards_in_the_base() -> None:
    conn = _Conn(set())
    await generate_source_id(conn, "sales pg")
    _, args = conn.queries[0]
    assert args == ("sales_pg", "sales\\_pg\\_%")
