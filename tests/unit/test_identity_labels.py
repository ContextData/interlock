"""Every audit view names the identity behind a row, including deleted ones."""

from __future__ import annotations

import pytest

from interlock.admin.identity_labels import IDENTITY_NAME_COLUMNS, identity_joins, identity_label


@pytest.mark.parametrize(
    ("value", "name", "deleted", "expected"),
    [
        (1, "analyst-claude", False, "analyst-claude (#1)"),
        (7, "live-rc9-legacy", True, "live-rc9-legacy (#7, deleted)"),
        (7, None, False, "Deleted identity #7"),
        (7, "", True, "Deleted identity #7"),
        (None, None, False, "No identity"),
        ("", None, False, "No identity"),
    ],
)
def test_identity_label(value: object, name: object, deleted: bool, expected: str) -> None:
    assert identity_label(value, name, deleted) == expected


def test_joins_resolve_live_identities_and_tombstones() -> None:
    joins = identity_joins("a.identity_id")
    assert "LEFT JOIN identities i ON a.identity_id = i.id" in joins
    assert "LEFT JOIN identity_tombstones it ON a.identity_id = it.identity_id" in joins
    assert "COALESCE(i.name, it.name) AS identity_name" in IDENTITY_NAME_COLUMNS
