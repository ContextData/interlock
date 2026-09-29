"""Register the five live systems as governed InterLock sources, and remove them.

Two properties of this module matter more than the rest.

**No live credential is ever written to the control database.** Every
`connection_config` holds `env://` references, which the gateway resolves in
its own process. The control database, `/api/data-sources`, and anything that
dumps a source row therefore contain only variable *names*. A test asserts
this rather than trusting it.

**Teardown is a correctness requirement of the e2e suite, not hygiene.**
`tests/e2e/test_connector_governance_uniformity.py` enumerates every enabled
source and fires a query at each one. A `live_cert_*` row left behind means the
next `make test-e2e` sends traffic to a production S3 bucket, Slack workspace
and Google tenant. That is why teardown runs in a `finally`, why the sweeper
exists, and why the uniformity test gains an assertion that no `live_cert_%`
source is enabled.

The upstream user each source connects as is chosen to make governance
falsifiable. MySQL connects as `generic_full_user`, which holds ALL PRIVILEGES;
PostgreSQL as `generic_write_user`, which holds SELECT plus INSERT/UPDATE.
Using the *least* restricted upstream user available is deliberate: when a
request is refused, only InterLock can have refused it. Had the source
connected as a read-only upstream user, "InterLock blocked the write" would be
indistinguishable from "the database blocked the write", and the governed
tests would prove nothing.

**The gateway must be restarted after seeding.** `ConnectionManager` caches an
asyncpg pool per source and returns it without re-reading the registry, and
nothing invalidates it when a source's configuration changes. A
re-seed that changes a connection therefore has no effect on a running
gateway. `make live-up` handles this; seeding by hand does not.

    uv run python -m tests.live.support.seed
    uv run python -m tests.live.support.stack restart-gateway
    uv run python -m tests.live.support.seed --teardown
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys

import asyncpg

from interlock.admin.auth import hash_password
from interlock.connections.connectors import (
    role_templates_for_connector,
    source_metadata_for_connector,
)
from interlock.core.auth import AuthManager
from tests.live.support.config import (
    AGENT_IDENTITY,
    BLOCKED_IDENTITY,
    POLICY_PREFIX,
    SOURCE_ID_PREFIX,
    LiveConfig,
    load_live_config,
)
from tests.live.support.credentials import export_environment, load_credentials

SEED_TAG = "live-cert"

AGENT_API_KEY = "ag-live-cert-agent-key"
BLOCKED_API_KEY = "ag-live-cert-blocked-key"

# The upstream identity each governed source connects as. See the module
# docstring: least restricted available, so a refusal is unambiguously ours.
PG_UPSTREAM_USER = "generic_write_user"
MYSQL_UPSTREAM_USER = "generic_full_user"


def source_id(system: str) -> str:
    return f"{SOURCE_ID_PREFIX}{system}"


def _source_rows(cfg: LiveConfig) -> list[tuple[str, str, str, dict, str]]:
    """(source_id, name, source_type, connection_config, connector_key).

    Only systems whose credentials are present are included, so a partial
    credential set registers a partial stack rather than failing.
    """
    rows: list[tuple[str, str, str, dict, str]] = []

    if cfg.has_postgres():
        rows.append(
            (
                source_id("postgres"),
                "Live Certification PostgreSQL",
                "postgresql",
                {
                    "host": cfg.pg_host,
                    "port": cfg.pg_port,
                    "database": cfg.pg_database,
                    "user": PG_UPSTREAM_USER,
                    "password_ref": "env://INTERLOCK_LIVE_PG_PASSWORD_GENERIC_WRITE_USER",
                    # `sslmode` alone, deliberately: it is the spelling the
                    # operator guide documents, and certifying the documented
                    # form is the point. It used to work on PG-wire and be
                    # silently ignored on the MCP path, so a source written
                    # this way connected unencrypted there (now fixed - both paths read it through one accessor).
                    "sslmode": "require",
                    # The managed cluster presents a certificate this stack has
                    # no CA bundle for. Transport security of the *upstream*
                    # hop is not what these tests certify; governance is.
                    "verify_ssl": False,
                },
                "postgresql",
            )
        )

    if cfg.has_mysql():
        rows.append(
            (
                source_id("mysql"),
                "Live Certification MySQL",
                "mysql",
                {
                    "host": cfg.mysql_host,
                    "port": cfg.mysql_port,
                    "database": cfg.mysql_database,
                    "user": MYSQL_UPSTREAM_USER,
                    "password_ref": "env://INTERLOCK_LIVE_MYSQL_PASSWORD_GENERIC_FULL_USER",
                },
                "mysql",
            )
        )

    if cfg.has_s3():
        rows.append(
            (
                source_id("s3"),
                "Live Certification S3",
                "s3",
                {
                    "bucket": cfg.s3_bucket,
                    "prefix": cfg.s3_prefix,
                    "region_name": cfg.s3_region,
                    "aws_access_key_id_ref": "env://INTERLOCK_LIVE_S3_ACCESS_KEY_ID",
                    "aws_secret_access_key_ref": "env://INTERLOCK_LIVE_S3_SECRET_ACCESS_KEY",
                },
                "s3",
            )
        )

    if cfg.has_slack():
        rows.append(
            (
                source_id("slack"),
                "Live Certification Slack",
                "saas",
                {
                    "workspace": cfg.slack_channel_name or "live-cert",
                    "bot_token_ref": "env://INTERLOCK_LIVE_SLACK_BOT_TOKEN",
                    "channel_types": ["public_channel"],
                    "include_messages": True,
                },
                "slack",
            )
        )

    if cfg.has_google_workspace():
        rows.append(
            (
                source_id("google_workspace"),
                "Live Certification Google Workspace",
                "saas",
                {
                    "workspace_domain": cfg.gws_workspace_domain or "live-cert",
                    "enabled_services": ["drive"],
                    "service_account_json_ref": ("env://INTERLOCK_LIVE_GWS_SERVICE_ACCOUNT_JSON"),
                    # Deliberately no subject_user: impersonation is impossible
                    # for a consumer subject, so the service account acts as
                    # itself. See test_upstream_google_workspace.
                },
                "google_workspace",
            )
        )

    return rows


# Roles granted to the agent identity per connector. Source roles are
# additive - a grant confers access and never removes it - so where a
# connector separates reading from writing, the agent holds both. Without the
# write role, a governed write is refused by role scope before write safety
# can be observed at all, and the certification would record a gap that is
# really a seeding choice.
_AGENT_ROLES = {
    "postgresql": ["analyst", "write"],
    "mysql": ["writer"],
    "s3": ["reader"],
    "slack": ["knowledge_reader"],
    "google_workspace": ["workspace_reader"],
}


async def _upsert_sources(conn: asyncpg.Connection, cfg: LiveConfig) -> list[str]:
    registered: list[str] = []
    for sid, name, source_type, config, connector_key in _source_rows(cfg):
        await conn.execute(
            """
            INSERT INTO data_sources
                (source_id, name, source_type, connection_config, cache_strategy,
                 enabled, metadata)
            VALUES ($1, $2, $3, $4::jsonb, 'bypass', TRUE, $5::jsonb)
            ON CONFLICT (source_id) DO UPDATE
            SET name = EXCLUDED.name,
                source_type = EXCLUDED.source_type,
                connection_config = EXCLUDED.connection_config,
                cache_strategy = EXCLUDED.cache_strategy,
                enabled = TRUE,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            sid,
            name,
            source_type,
            json.dumps(config),
            json.dumps({"seed": SEED_TAG, **source_metadata_for_connector(connector_key)}),
        )
        registered.append(sid)
    return registered


async def _upsert_identity(
    conn: asyncpg.Connection, *, name: str, api_key: str, roles: list[str]
) -> int:
    """Create the certifying identity.

    `agent_type` must be a member of the AgentType enum - claude_code, codex,
    copilot or custom. An unrecognised value makes the gateway fail to build
    an IdentityContext and answer 401 "Invalid API key", which points at the
    credential rather than at the malformed row. Established by seeding
    'certification' here and watching a correctly-hashed key be rejected.
    """
    return int(
        await conn.fetchval(
            """
            INSERT INTO identities
                (name, api_key_hash, agent_type, team, roles, mapped_pg_role,
                 enabled, metadata, pg_username, pg_password_hash, auth_metadata)
            VALUES ($1, $2, 'custom', 'live-cert', $3, NULL,
                    TRUE, $4::jsonb, $5, $6, $4::jsonb)
            ON CONFLICT (api_key_hash) DO UPDATE
            SET name = EXCLUDED.name,
                agent_type = EXCLUDED.agent_type,
                team = EXCLUDED.team,
                roles = EXCLUDED.roles,
                mapped_pg_role = EXCLUDED.mapped_pg_role,
                enabled = TRUE,
                metadata = EXCLUDED.metadata,
                auth_metadata = EXCLUDED.auth_metadata,
                pg_username = EXCLUDED.pg_username,
                pg_password_hash = EXCLUDED.pg_password_hash,
                updated_at = NOW()
            RETURNING id
            """,
            name,
            AuthManager.hash_api_key(api_key),
            sorted(set(roles)),
            json.dumps({"seed": SEED_TAG}),
            f"{name.replace('-', '_')}_pg",
            hash_password("live-cert-pg-password"),
        )
    )


async def _replace_permissions(
    conn: asyncpg.Connection, role_id: int, permissions: list[dict]
) -> None:
    await conn.execute("DELETE FROM source_role_permissions WHERE role_id = $1", role_id)
    for permission in permissions:
        await conn.execute(
            """
            INSERT INTO source_role_permissions
                (role_id, effect, action, resource_type, resource_pattern, constraints)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            role_id,
            permission.get("effect", "allow"),
            permission.get("action", "*"),
            permission.get("resource_type", "*"),
            permission.get("resource_pattern", "*"),
            json.dumps(permission.get("constraints") or {}),
        )


async def _upsert_roles_and_grants(
    conn: asyncpg.Connection, cfg: LiveConfig, *, agent_id: int, blocked_id: int
) -> None:
    for sid, _name, source_type, _config, connector_key in _source_rows(cfg):
        agent_roles = _AGENT_ROLES[connector_key]
        for role_key in (*agent_roles, "blocked"):
            role_id = int(
                await conn.fetchval(
                    """
                    INSERT INTO source_roles
                        (source_id, role_key, name, description, review_required, metadata)
                    VALUES ($1, $2, initcap(replace($2, '_', ' ')),
                            'Live certification source role', FALSE, $3::jsonb)
                    ON CONFLICT (source_id, role_key) DO UPDATE
                    SET enabled = TRUE,
                        review_required = FALSE,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    sid,
                    role_key,
                    json.dumps({"seed": SEED_TAG}),
                )
            )
            if connector_key == "postgresql":
                # PostgreSQL permissions come from a SQL function rather than
                # the connector templates, matching the e2e seed.
                await conn.execute(
                    "SELECT agentgate_seed_source_role_permissions($1, $2, $3)",
                    role_id,
                    role_key,
                    source_type,
                )
            else:
                await _replace_permissions(
                    conn,
                    role_id,
                    role_templates_for_connector(connector_key).get(role_key, []),
                )

            identity_id = agent_id if role_key in agent_roles else blocked_id
            await conn.execute(
                """
                INSERT INTO identity_source_role_grants
                    (identity_id, source_id, role_id, metadata)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (identity_id, source_id, role_id) DO UPDATE
                SET enabled = TRUE, metadata = EXCLUDED.metadata, updated_at = NOW()
                """,
                identity_id,
                sid,
                role_id,
                json.dumps({"seed": SEED_TAG}),
            )


async def _upsert_policies(conn: asyncpg.Connection, cfg: LiveConfig) -> None:
    """Mirror the e2e policy shape: a high-priority deny and a redacting allow.

    `policy_rules` has no metadata column and keys its conditions on
    `source_ids` and `roles` rather than identity names - matched to the real
    schema rather than to what seemed natural. The deny sits at priority 100
    and the allow at 10, so the deny wins wherever both match, which is the
    ordering the authorization tests rely on.
    """
    await conn.execute("DELETE FROM policy_rules WHERE name LIKE $1", f"{POLICY_PREFIX}%")
    source_ids = [sid for sid, *_ in _source_rows(cfg)]
    agent_roles = sorted({r for *_, key in _source_rows(cfg) for r in _AGENT_ROLES[key]})

    await conn.execute(
        """
        INSERT INTO policy_rules (name, priority, enabled, conditions, actions)
        VALUES ($1, 100, TRUE, $2::jsonb, $3::jsonb)
        """,
        f"{POLICY_PREFIX}deny-blocked",
        json.dumps({"source_ids": source_ids, "roles": ["blocked"]}),
        json.dumps({"effect": "deny"}),
    )
    await conn.execute(
        """
        INSERT INTO policy_rules (name, priority, enabled, conditions, actions)
        VALUES ($1, 10, TRUE, $2::jsonb, $3::jsonb)
        """,
        f"{POLICY_PREFIX}allow-agent",
        json.dumps({"source_ids": source_ids, "roles": agent_roles}),
        json.dumps({"effect": "allow", "redact_columns": ["ssn", "email"]}),
    )


async def _notify(conn: asyncpg.Connection) -> None:
    await conn.execute("SELECT pg_notify('onyx_config_changed', 'live-cert')")
    await conn.execute("SELECT pg_notify('onyx_policy_changed', 'live-cert')")


async def _clear_cached_sessions() -> None:
    """Drop cached sessions after re-seeding identities.

    Teardown deletes identity rows and seeding recreates them with new ids, so
    a session cached against the old row survives in Redis and revalidates to
    nothing - producing a 401 on the first requests after a re-seed, which
    looks exactly like a governance refusal and made
    `test_s3_refuses_a_blocked_identity` fail intermittently with 401 instead
    of 403.

    Clearing here rather than tolerating a retry is deliberate: this suite
    treats a control that works only sometimes as a failure, so its own
    fixtures must not be the source of the "sometimes".
    """
    subprocess.run(
        [
            "docker",
            "exec",
            "interlock-e2e-redis-1",
            "sh",
            "-c",
            "redis-cli --scan --pattern 'session:*' | xargs -r redis-cli DEL",
        ],
        capture_output=True,
        check=False,
    )


async def seed_all(cfg: LiveConfig | None = None) -> list[str]:
    cfg = cfg or load_live_config()
    conn = await asyncpg.connect(cfg.e2e.control_dsn)
    try:
        registered = await _upsert_sources(conn, cfg)
        agent_id = await _upsert_identity(
            conn,
            name=AGENT_IDENTITY,
            api_key=AGENT_API_KEY,
            roles=[r for *_, key in _source_rows(cfg) for r in _AGENT_ROLES[key]],
        )
        blocked_id = await _upsert_identity(
            conn, name=BLOCKED_IDENTITY, api_key=BLOCKED_API_KEY, roles=["blocked"]
        )
        await _upsert_roles_and_grants(conn, cfg, agent_id=agent_id, blocked_id=blocked_id)
        await _upsert_policies(conn, cfg)
        await _notify(conn)
        await _clear_cached_sessions()
        return registered
    finally:
        await conn.close()


async def teardown_all(cfg: LiveConfig | None = None) -> dict[str, int]:
    """Remove every control-plane object this harness creates.

    Ordered by foreign key dependency. Never touches `audit_log` - those rows
    are the evidence the certification rests on.
    """
    cfg = cfg or load_live_config()
    conn = await asyncpg.connect(cfg.e2e.control_dsn)
    removed: dict[str, int] = {}
    try:
        like = f"{SOURCE_ID_PREFIX}%"

        async def _count(sql: str, *args: object) -> int:
            status = await conn.execute(sql, *args)
            return int(status.rsplit(" ", 1)[-1]) if status.rsplit(" ", 1)[-1].isdigit() else 0

        removed["grants"] = await _count(
            "DELETE FROM identity_source_role_grants WHERE source_id LIKE $1", like
        )
        removed["permissions"] = await _count(
            "DELETE FROM source_role_permissions WHERE role_id IN "
            "(SELECT id FROM source_roles WHERE source_id LIKE $1)",
            like,
        )
        removed["roles"] = await _count("DELETE FROM source_roles WHERE source_id LIKE $1", like)
        removed["policies"] = await _count(
            "DELETE FROM policy_rules WHERE name LIKE $1", f"{POLICY_PREFIX}%"
        )
        removed["identities"] = await _count(
            "DELETE FROM identities WHERE name IN ($1, $2)", AGENT_IDENTITY, BLOCKED_IDENTITY
        )
        removed["sources"] = await _count("DELETE FROM data_sources WHERE source_id LIKE $1", like)
        await _notify(conn)
        return removed
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teardown", action="store_true", help="remove instead of register")
    args = parser.parse_args(argv)

    export_environment(load_credentials())
    if args.teardown:
        removed = asyncio.run(teardown_all())
        print("removed:", ", ".join(f"{k}={v}" for k, v in removed.items()))
    else:
        registered = asyncio.run(seed_all())
        print(f"registered {len(registered)} live source(s): {', '.join(registered)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
