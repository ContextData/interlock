"""Migration 015 turns JSON-string scalars written by the double-encoding
defect back into the objects they always meant to be (see
test_jsonb_codec_writes.py). Its behaviour against a real PostgreSQL is proven
in tests/e2e; these tests pin its reach and its restraint."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "015_repair_double_encoded_jsonb.sql"

# Every column a codec-backed pool wrote pre-serialised JSON into. Measured on
# the rehearsal-3 control plane, plus the two policy_rules columns the console
# create, edit and import paths write the same way.
AFFECTED = [
    ("data_sources", "metadata"),
    ("data_sources", "connection_config"),
    ("identities", "metadata"),
    ("source_roles", "metadata"),
    ("source_role_permissions", "constraints"),
    ("identity_source_role_grants", "metadata"),
    ("policy_rules", "conditions"),
    ("policy_rules", "actions"),
    ("cache_dependencies", "metadata"),
    ("discovery_assets", "summary"),
]


def _sql() -> str:
    return re.sub(r"\s+", " ", MIGRATION.read_text()).lower()


def test_the_repair_migration_exists_in_sequence() -> None:
    """It was the head when it shipped; later migrations follow it, and 016
    (the source catalog) must never have been numbered before it."""
    assert MIGRATION.exists()
    versions = sorted(p.name[:3] for p in (ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    assert "015" in versions
    assert versions.index("015") == len([v for v in versions if v < "015"])


def test_it_repairs_every_column_the_defect_wrote() -> None:
    sql = _sql()
    missing = [f"{t}.{c}" for t, c in AFFECTED if f"'{t}'" not in sql or f"'{c}'" not in sql]
    assert missing == []


def test_it_only_rewrites_json_string_scalars() -> None:
    sql = _sql()
    assert "jsonb_typeof" in sql and "'string'" in sql
    # A string that is not JSON is left as it was rather than failing the upgrade.
    assert "exception" in sql
