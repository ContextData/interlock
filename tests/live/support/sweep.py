"""Delete certification artifacts a killed run left behind.

Teardown runs in a `finally`, but `finally` does not survive SIGKILL, a lost
network, or a laptop closing. These are shared production systems, so
something has to clean up after the case teardown cannot cover. Every artifact
this harness creates is named from `ARTIFACT_PREFIX` and a run stamp, which
exists precisely so a later process can recognise them.

Deliberately conservative: it removes only names matching the certification
prefix, and by default only those older than a cutoff, so it cannot delete an
artifact belonging to a run happening right now.

    uv run python -m tests.live.support.sweep
    uv run python -m tests.live.support.sweep --older-than 0   # everything
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import UTC, datetime, timedelta

import asyncpg

from tests.live.support import effects
from tests.live.support.config import (
    ARTIFACT_PREFIX,
    ARTIFACT_PREFIX_DASHED,
    SOURCE_ID_PREFIX,
    LiveConfig,
    load_live_config,
)
from tests.live.support.credentials import export_environment, load_credentials

# Run ids are `lc<YYYYMMDDHHMMSS><6 hex>`; the timestamp is what makes an
# artifact's age recoverable from its name alone.
_RUN_ID = re.compile(r"lc(\d{14})[0-9a-f]{6}")


def _age_hours(name: str) -> float | None:
    match = _RUN_ID.search(name)
    if not match:
        return None
    try:
        stamped = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - stamped).total_seconds() / 3600


def _is_stale(name: str, older_than_hours: float) -> bool:
    if older_than_hours <= 0:
        return True
    age = _age_hours(name)
    # An unparseable stamp is left alone. Guessing wrong on a shared system is
    # worse than leaving one file behind for a human to look at.
    return age is not None and age >= older_than_hours


async def sweep_postgres(cfg: LiveConfig, older_than: float) -> list[str]:
    rows = await effects.postgres_rows(
        cfg,
        "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE $1",
        f"{ARTIFACT_PREFIX}%",
        user="generic_ddl_user",
    )
    removed: list[str] = []
    for row in rows:
        name = str(row["tablename"])
        if _is_stale(name, older_than):
            await effects.postgres_execute(
                cfg, f"DROP TABLE IF EXISTS {name} CASCADE", user="generic_ddl_user"
            )
            removed.append(name)
    return removed


async def sweep_mysql(cfg: LiveConfig, older_than: float) -> list[str]:
    rows = await effects.mysql_rows(
        cfg,
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name LIKE %s",
        cfg.mysql_database,
        f"{ARTIFACT_PREFIX}%",
        user="generic_ddl_user",
    )
    removed: list[str] = []
    for row in rows:
        name = str(row["t"])
        if _is_stale(name, older_than):
            await effects.mysql_execute(
                cfg, f"DROP TABLE IF EXISTS `{name}`", user="generic_ddl_user"
            )
            removed.append(name)
    return removed


def sweep_s3(cfg: LiveConfig, older_than: float) -> list[str]:
    prefix = f"{cfg.s3_prefix}{ARTIFACT_PREFIX_DASHED}/"
    keys = effects.s3_object_keys(cfg, prefix=prefix)
    removed: list[str] = []
    for key in keys:
        if _is_stale(key, older_than):
            effects.s3_delete_prefix(cfg, key)
            removed.append(key)
    return removed


def sweep_slack(cfg: LiveConfig, older_than: float) -> list[str]:
    """Slack messages are found by the run marker embedded in their text."""
    from slack_sdk import WebClient

    client = WebClient(token=cfg.slack_bot_token)
    response = client.conversations_history(channel=cfg.slack_channel_id, limit=100)
    removed: list[str] = []
    for message in response.get("messages", []):
        text = str(message.get("text", ""))
        if ARTIFACT_PREFIX_DASHED not in text or not _is_stale(text, older_than):
            continue
        try:
            client.chat_delete(channel=cfg.slack_channel_id, ts=message["ts"])
            removed.append(str(message["ts"]))
        except Exception:  # noqa: BLE001 - a message someone else owns, or already gone
            continue
    return removed


async def sweep_control_plane(cfg: LiveConfig) -> int:
    """Always total, never age-based.

    Control-plane rows are configuration rather than stamped artifacts, and a
    surviving `live_cert_*` source is the one leak that actively harms: the
    e2e suite queries every enabled source, so leaving one points `make
    test-e2e` at production.
    """
    from tests.live.support.seed import teardown_all

    removed = await teardown_all(cfg)
    conn = await asyncpg.connect(cfg.e2e.control_dsn)
    try:
        await conn.execute(
            "DELETE FROM discovery_assets WHERE source_id LIKE $1", f"{SOURCE_ID_PREFIX}%"
        )
    finally:
        await conn.close()
    return sum(removed.values())


async def sweep_all(cfg: LiveConfig, older_than: float) -> dict[str, list[str] | int]:
    results: dict[str, list[str] | int] = {}
    if cfg.has_postgres():
        results["postgres_tables"] = await sweep_postgres(cfg, older_than)
    if cfg.has_mysql():
        results["mysql_tables"] = await sweep_mysql(cfg, older_than)
    if cfg.has_s3():
        results["s3_objects"] = sweep_s3(cfg, older_than)
    if cfg.has_slack():
        results["slack_messages"] = sweep_slack(cfg, older_than)
    results["control_plane_rows"] = await sweep_control_plane(cfg)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than",
        type=float,
        default=0.0,
        metavar="HOURS",
        help="only remove artifacts at least this old; 0 (default) removes all",
    )
    args = parser.parse_args(argv)

    export_environment(load_credentials())
    cfg = load_live_config()
    results = asyncio.run(sweep_all(cfg, args.older_than))

    total = 0
    for name, value in results.items():
        count = value if isinstance(value, int) else len(value)
        total += count
        print(f"  {name:<20} {count}")
    print(f"swept {total} artifact(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())


__all__ = ["sweep_all", "timedelta"]
