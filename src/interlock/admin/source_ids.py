"""Source ids generated from a source's display name.

The source id is what agents and their users type: the PostgreSQL database
name (`dbname=sales_postgresql`), the HTTP proxy path segment and the MCP
`source_id` argument. It cannot change once created, because every role,
grant, catalog row and audit entry refers to it. So it is derived once from
the display name, readable, and never asked of the person registering the
source.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Lower-case, starts with a letter or digit, at most 63 characters: a valid
# unquoted-safe PostgreSQL database name and a single URL path segment.
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

_MAX_BASE = 48
_FALLBACK = "source"


class InvalidSourceIdError(ValueError):
    """A supplied source id is not in the accepted form."""


def slugify_source_id(name: str) -> str:
    """`"Sales PostgreSQL"` -> `"sales_postgresql"`; never empty."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")
    slug = slug[:_MAX_BASE].rstrip("_")
    return slug or _FALLBACK


def validate_source_id(value: str) -> str:
    """Return `value` if it is an acceptable source id, else raise."""
    if not SOURCE_ID_RE.fullmatch(value):
        raise InvalidSourceIdError(
            "source_id must be 1-63 characters of lower-case letters, digits, "
            "'_' or '-', starting with a letter or digit."
        )
    return value


async def generate_source_id(conn: Any, name: str) -> str:
    """The display name's slug, suffixed `_2`, `_3`... until it is unused."""
    base = slugify_source_id(name)
    taken = {
        str(row["source_id"])
        for row in await conn.fetch(
            "SELECT source_id FROM data_sources WHERE source_id = $1 OR source_id LIKE $2",
            base,
            base.replace("_", "\\_") + "\\_%",
        )
    }
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"
