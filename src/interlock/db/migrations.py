"""Production migration runner for InterLock.

The Docker entrypoint can initialize a brand-new PostgreSQL volume, but MVP
deployments need a repeatable runner that also works for already-created
databases. This module records applied migration checksums, uses a PostgreSQL
advisory lock, and can baseline databases that were initialized by the legacy
``docker-entrypoint-initdb.d`` path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
MIGRATION_LOCK_KEY = 0x0A61_06A7
LEGACY_VERSION_MARKERS: tuple[tuple[str, str, str, str | None], ...] = (
    ("012", "column", "identities", "api_key_hash_version"),
    ("011", "column", "audit_log", "event_id"),
    ("010", "table", "audit_partition_maintenance", None),
    ("008", "index", "idx_identity_source_role_grants_active", None),
    ("007", "table", "source_roles", None),
    ("006", "column", "identities", "pg_username"),
    ("005", "table", "cache_dependencies", None),
    ("004", "table", "alert_rules", None),
    ("003", "column", "identities", "last_used_at"),
    ("002", "table", "admin_identities", None),
)


@dataclass(frozen=True, slots=True)
class MigrationFile:
    version: str
    name: str
    path: Path
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: list[str]
    skipped: list[str]
    baselined: list[str]


async def verify_migration_head(
    connection_or_pool: Any,
    migrations_dir: str | Path,
) -> dict[str, str | int]:
    """Verify every bundled migration is recorded with the expected checksum.

    This is deliberately read-only so readiness can fail without attempting
    schema changes from an application process. Deployment migration jobs own
    mutation; serving processes only prove that their code and schema agree.
    """
    migrations = discover_migrations(migrations_dir)
    rows = await connection_or_pool.fetch(
        f"SELECT version, checksum FROM {SCHEMA_MIGRATIONS_TABLE}"
    )
    applied = {str(row["version"]): str(row["checksum"]) for row in rows}
    for migration in migrations:
        checksum = applied.get(migration.version)
        if checksum is None:
            raise RuntimeError(f"required migration {migration.version} is not applied")
        if checksum != migration.checksum:
            raise RuntimeError(f"migration {migration.version} checksum does not match")
    return {
        "status": "ok",
        "head": migrations[-1].version,
        "applied": len(applied),
    }


def discover_migrations(migrations_dir: str | Path) -> list[MigrationFile]:
    """Return SQL migrations in lexicographic order."""
    root = Path(migrations_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {root}")

    migrations: list[MigrationFile] = []
    for path in sorted(root.glob("*.sql")):
        version, _, name = path.name.partition("_")
        if not version or not name:
            raise ValueError(f"migration filename must start with '<version>_': {path.name}")
        payload = path.read_bytes()
        migrations.append(
            MigrationFile(
                version=version,
                name=path.name,
                path=path,
                checksum=hashlib.sha256(payload).hexdigest(),
            )
        )
    if not migrations:
        raise ValueError(f"no SQL migrations found in {root}")
    return migrations


async def apply_migrations(
    connection: Any,
    migrations_dir: str | Path,
    *,
    baseline_existing: bool = True,
) -> MigrationResult:
    """Apply pending migrations using an advisory lock and checksum table."""
    migrations = discover_migrations(migrations_dir)
    await _ensure_schema_migrations(connection)

    await connection.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
    try:
        applied_rows = await connection.fetch(
            f"SELECT version, checksum FROM {SCHEMA_MIGRATIONS_TABLE}"
        )
        applied = {str(row["version"]): str(row["checksum"]) for row in applied_rows}

        result = MigrationResult(applied=[], skipped=[], baselined=[])
        baselined_versions: set[str] = set()
        if baseline_existing and not applied:
            legacy_version = await _detect_legacy_version(connection)
            if legacy_version is not None:
                baselined_migrations = [
                    migration for migration in migrations if migration.version <= legacy_version
                ]
                baseline = await _baseline(connection, baselined_migrations)
                result.baselined.extend(baseline.baselined)
                baselined_versions = {migration.version for migration in baselined_migrations}
                applied = {
                    migration.version: migration.checksum for migration in baselined_migrations
                }

        for migration in migrations:
            recorded_checksum = applied.get(migration.version)
            if recorded_checksum is not None:
                if recorded_checksum != migration.checksum:
                    raise RuntimeError(
                        "Migration checksum mismatch for "
                        f"{migration.version}: expected {recorded_checksum}, "
                        f"found {migration.checksum}"
                    )
                if migration.version in baselined_versions:
                    continue
                result.skipped.append(migration.name)
                continue

            sql = migration.path.read_text()
            async with connection.transaction():
                await connection.execute(sql)
                await connection.execute(
                    f"""
                    INSERT INTO {SCHEMA_MIGRATIONS_TABLE}
                        (version, name, checksum, applied_at)
                    VALUES ($1, $2, $3, NOW())
                    """,
                    migration.version,
                    migration.name,
                    migration.checksum,
                )
            result.applied.append(migration.name)
        return result
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)


async def _ensure_schema_migrations(connection: Any) -> None:
    await connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """)


async def _looks_initialized(connection: Any) -> bool:
    return bool(await connection.fetchval("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('data_sources', 'identities', 'audit_log')
            )
            """))


async def _table_exists(connection: Any, table_name: str) -> bool:
    return bool(
        await connection.fetchval(
            """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = $1
        )
        """,
            table_name,
        )
    )


async def _column_exists(connection: Any, table_name: str, column_name: str) -> bool:
    return bool(
        await connection.fetchval(
            """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = $1
              AND column_name = $2
        )
        """,
            table_name,
            column_name,
        )
    )


async def _index_exists(connection: Any, index_name: str) -> bool:
    return bool(
        await connection.fetchval(
            """
        SELECT EXISTS (
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = $1
        )
        """,
            index_name,
        )
    )


async def _detect_legacy_version(connection: Any) -> str | None:
    for version, marker_type, object_name, column_name in LEGACY_VERSION_MARKERS:
        if marker_type == "column":
            if column_name is None:
                continue
            exists = await _column_exists(connection, object_name, column_name)
        elif marker_type == "index":
            exists = await _index_exists(connection, object_name)
        else:
            exists = await _table_exists(connection, object_name)
        if exists:
            return version
    if await _looks_initialized(connection):
        return "001"
    return None


async def _baseline(
    connection: Any,
    migrations: list[MigrationFile],
) -> MigrationResult:
    baselined: list[str] = []
    async with connection.transaction():
        for migration in migrations:
            await connection.execute(
                f"""
                INSERT INTO {SCHEMA_MIGRATIONS_TABLE}
                    (version, name, checksum, applied_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (version) DO NOTHING
                """,
                migration.version,
                migration.name,
                migration.checksum,
            )
            baselined.append(migration.name)
    return MigrationResult(applied=[], skipped=[], baselined=baselined)
