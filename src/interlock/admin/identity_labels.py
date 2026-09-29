"""One way to name the identity behind an audit, approval or alert row.

`audit_log` and `write_approval_queue` keep only `identity_id`, and deleting an
identity removes its row, so a view that joined `identities` alone showed a
bare number for anything a deleted identity did. Deletion now leaves a row in
`identity_tombstones` (migration 017); every view resolves names through both.
"""

from __future__ import annotations

from typing import Any

# Columns to select alongside `identity_joins(...)`.
IDENTITY_NAME_COLUMNS = (
    "COALESCE(i.name, it.name) AS identity_name, "
    "(i.id IS NULL AND it.identity_id IS NOT NULL) AS identity_deleted"
)


def identity_joins(column: str) -> str:
    """LEFT JOINs resolving `column` against live identities and tombstones."""
    return (
        f"LEFT JOIN identities i ON {column} = i.id "
        f"LEFT JOIN identity_tombstones it ON {column} = it.identity_id"
    )


def identity_label(value: Any, name: Any = None, deleted: Any = False) -> str:
    """Human label for an identity reference.

    Names are not unique, so the id stays beside the name.
    """
    has_id = value not in (None, "")
    has_name = name not in (None, "") and bool(str(name))
    if not has_id:
        return str(name) if has_name else "No identity"
    if not has_name:
        return f"Deleted identity #{value}"
    if deleted:
        return f"{name} (#{value}, deleted)"
    return f"{name} (#{value})"
