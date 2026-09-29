"""CLI entrypoint for applying database migrations."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import asyncpg

from interlock.db.migrations import apply_migrations


def _database_url_from_env() -> str | None:
    if database_url := os.environ.get("DATABASE_URL"):
        return database_url
    host = os.environ.get("INTERLOCK_DATABASE__HOST")
    database = os.environ.get("INTERLOCK_DATABASE__DATABASE")
    user = os.environ.get("INTERLOCK_DATABASE__USER")
    password = os.environ.get("INTERLOCK_DATABASE__PASSWORD")
    if not all([host, database, user, password]):
        return None
    port = os.environ.get("INTERLOCK_DATABASE__PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _default_migrations_dir() -> Path:
    configured = os.environ.get("INTERLOCK_MIGRATIONS_DIR")
    if configured:
        return Path(configured)
    image_path = Path("/app/migrations")
    if image_path.exists():
        return image_path
    packaged_path = Path(__file__).resolve().parents[1] / "migrations"
    if packaged_path.exists():
        return packaged_path
    return Path(__file__).resolve().parents[3] / "migrations"


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Apply InterLock database migrations")
    parser.add_argument(
        "--migrations-dir",
        default=str(_default_migrations_dir()),
        help="Directory containing ordered .sql migration files",
    )
    parser.add_argument(
        "--no-baseline-existing",
        action="store_true",
        help="Do not baseline an already-initialized legacy database",
    )
    args = parser.parse_args()

    database_url = _database_url_from_env()
    if not database_url:
        raise SystemExit("DATABASE_URL or INTERLOCK_DATABASE__* settings are required")

    conn = await asyncpg.connect(database_url)
    try:
        result = await apply_migrations(
            conn,
            args.migrations_dir,
            baseline_existing=not args.no_baseline_existing,
        )
    finally:
        await conn.close()

    print(
        "Migrations complete: "
        f"applied={len(result.applied)} "
        f"skipped={len(result.skipped)} "
        f"baselined={len(result.baselined)}"
    )
    if result.applied:
        print("Applied: " + ", ".join(result.applied))
    if result.baselined:
        print("Baselined: " + ", ".join(result.baselined))


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
