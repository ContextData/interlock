"""Shared access to the documentation site's pages for doc-pinning tests.

Tests that pin a phrase in a page read it through `page()`, so moving a page
changes one route here rather than a path in every test.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs-site"
CONTENT = SITE / "src" / "content" / "docs"


def all_pages() -> list[Path]:
    return sorted(p for p in CONTENT.rglob("*") if p.suffix in {".md", ".mdx"})


def resolve_route(route: str) -> Path:
    """Map a site route such as `/reference/configuration/` to its source file."""
    slug = route.strip("/")
    for candidate in (
        CONTENT / f"{slug}.md",
        CONTENT / f"{slug}.mdx",
        CONTENT / slug / "index.md",
        CONTENT / slug / "index.mdx",
    ):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"no page for route {route!r}")


def split(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, text[match.end() :]


def frontmatter(route: str) -> dict[str, object]:
    return split(resolve_route(route))[0]


def body(route: str) -> str:
    return split(resolve_route(route))[1]


def page(route: str) -> str:
    """A page's body with whitespace collapsed, for phrase assertions."""
    return " ".join(body(route).split())
