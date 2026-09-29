#!/usr/bin/env python3
"""Seed a minimal end-to-end demo into a running Onyx stack.

Creates one data source, one identity (API key), and one allow-all
policy so an agent can immediately connect and exercise the proxy.

Usage:
    docker compose up -d --wait
    python scripts/seed_demo.py [--api-key demo-key] [--source-id onyx-self]

Writes are done directly to the Onyx state database (default
``postgres://onyx:onyx_dev@localhost:5433/onyx``) so this script does
not need an authenticated admin session. A NOTIFY is fired on each
channel so the gateway reloads its in-memory caches.

Override the DSN with ``ONYX_STATE_DSN`` if you have moved Postgres.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os

import asyncpg

DEFAULT_DSN = os.environ.get("ONYX_STATE_DSN", "postgres://onyx:onyx_dev@localhost:5433/onyx")


async def seed(api_key: str, source_id: str, upstream_host: str, upstream_port: int) -> None:
    conn = await asyncpg.connect(DEFAULT_DSN)
    try:
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        # 1) Identity
        identity_id = await conn.fetchval(
            """
            INSERT INTO identities (name, agent_type, team, roles, mapped_pg_role,
                                    api_key_hash, enabled)
            VALUES ('demo-agent', 'custom', 'demo',
                    ARRAY['analyst', 'reader']::TEXT[],
                    'onyx', $1, TRUE)
            ON CONFLICT (api_key_hash) DO UPDATE
                SET enabled = TRUE
            RETURNING id
            """,
            api_key_hash,
        )
        print(f"  identity_id  = {identity_id}  (api_key={api_key!r})")

        # 2) Data source
        cfg = json.dumps({"host": upstream_host, "port": upstream_port})
        await conn.execute(
            """
            INSERT INTO data_sources (source_id, name, source_type,
                                      connection_config, cache_strategy, enabled)
            VALUES ($1, 'Demo Source', 'postgresql', $2::jsonb,
                    'deterministic_first', TRUE)
            ON CONFLICT (source_id) DO UPDATE
                SET enabled = TRUE,
                    connection_config = EXCLUDED.connection_config
            """,
            source_id,
            cfg,
        )
        print(f"  source_id    = {source_id!r}  -> {upstream_host}:{upstream_port}")

        # 3) Allow-all policy for the analyst role
        await conn.execute(
            """
            INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
            VALUES ('demo-allow-analyst', 10,
                    $1::jsonb, $2::jsonb, TRUE)
            ON CONFLICT (name) DO UPDATE
                SET conditions = EXCLUDED.conditions,
                    actions    = EXCLUDED.actions,
                    enabled    = TRUE
            """,
            json.dumps({"roles": ["analyst"]}),
            json.dumps({"effect": "allow"}),
        )
        print("  policy       = 'demo-allow-analyst' (allow for role=analyst)")

        # 4) Tell the gateway to reload registry + policy.
        await conn.execute("NOTIFY onyx_config_changed")
        await conn.execute("NOTIFY onyx_policy_changed")
        print("  NOTIFY sent  -> gateway will reload registry + policy")
    finally:
        await conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--api-key",
        default="demo-key",
        help="raw API key the agent will present (default: demo-key)",
    )
    p.add_argument(
        "--source-id",
        default="onyx-self",
        help="logical source name the agent connects to (default: onyx-self)",
    )
    p.add_argument(
        "--upstream-host",
        default="postgres",
        help="hostname of the upstream PG the proxy forwards to. "
        "Use 'postgres' inside compose or 'localhost' if running "
        "against an external DB on the host. (default: postgres)",
    )
    p.add_argument(
        "--upstream-port", type=int, default=5432, help="port of the upstream PG (default: 5432)"
    )
    args = p.parse_args()

    print("Seeding Onyx demo state:")
    asyncio.run(seed(args.api_key, args.source_id, args.upstream_host, args.upstream_port))
    print(
        "\nDone. Try it out:\n"
        "  psql 'postgres://{key}@localhost:5432/{src}'\n"
        "  curl -H 'Authorization: Bearer {key}' -X POST \\\n"
        "       -H 'Content-Type: application/json' \\\n"
        "       http://localhost:3001/mcp/tools/call \\\n"
        '       -d \'{{"name":"list_sources","arguments":{{}}}}\''.format(
            key=args.api_key, src=args.source_id
        )
    )


if __name__ == "__main__":
    main()
