#!/usr/bin/env python3
"""Registered live-source certification against the local Onyx stack.

This runner registers the supplied live systems in the local control plane,
creates source roles and grants, exercises Gateway/MCP and connector paths,
and writes a redacted certification report.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from interlock.admin.auth import hash_password
from interlock.connections.connectors import (
    get_adapter,
    role_templates_for_connector,
    source_metadata_for_connector,
)
from interlock.core.auth import AuthManager

_CREDENTIAL_RUNNER = Path(__file__).with_name("live_credentials_certify.py")
_SPEC = importlib.util.spec_from_file_location("live_credentials_certify", _CREDENTIAL_RUNNER)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - startup guard
    raise RuntimeError(f"Unable to load {_CREDENTIAL_RUNNER}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

LiveCredentials = _MODULE.LiveCredentials
Redactor = _MODULE.Redactor
Result = _MODULE.Result
add = _MODULE.add
load_credentials = _MODULE.load_credentials


CONTROL_DSN = "postgresql://onyx:onyx_dev@127.0.0.1:5433/onyx"
GATEWAY_URL = "http://127.0.0.1:3001"
ADMIN_URL = "http://127.0.0.1:9090"


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


async def _connect_control():
    import asyncpg

    return await asyncpg.connect(CONTROL_DSN)


async def _replace_permissions(conn: Any, role_id: int, permissions: list[dict[str, Any]]) -> None:
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
            _json(permission.get("constraints") or {}),
        )


async def _upsert_role(
    conn: Any,
    *,
    source_id: str,
    role_key: str,
    connector_key: str,
    permissions: list[dict[str, Any]] | None = None,
) -> int:
    role_id = await conn.fetchval(
        """
        INSERT INTO source_roles
            (source_id, role_key, name, description, review_required, metadata)
        VALUES ($1, $2, initcap(replace($2, '_', ' ')),
                'Live certification role', FALSE, $3::jsonb)
        ON CONFLICT (source_id, role_key) DO UPDATE
        SET name = EXCLUDED.name,
            description = EXCLUDED.description,
            enabled = TRUE,
            review_required = FALSE,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        RETURNING id
        """,
        source_id,
        role_key,
        _json({"seed": "live-cert", "connector_key": connector_key}),
    )
    await _replace_permissions(
        conn,
        int(role_id),
        (
            permissions
            if permissions is not None
            else role_templates_for_connector(connector_key).get(role_key, [])
        ),
    )
    return int(role_id)


async def _upsert_identity(conn: Any, *, api_key: str, pg_username: str, pg_password: str) -> int:
    return int(
        await conn.fetchval(
            """
            INSERT INTO identities
                (name, api_key_hash, agent_type, team, roles, enabled, metadata,
                 pg_username, pg_password_hash, auth_metadata)
            VALUES ($1, $2, 'codex', 'certification', $3::text[], TRUE, $4::jsonb,
                    $5, $6, $7::jsonb)
            ON CONFLICT (api_key_hash) DO UPDATE
            SET name = EXCLUDED.name,
                agent_type = EXCLUDED.agent_type,
                team = EXCLUDED.team,
                roles = EXCLUDED.roles,
                enabled = TRUE,
                metadata = EXCLUDED.metadata,
                pg_username = EXCLUDED.pg_username,
                pg_password_hash = EXCLUDED.pg_password_hash,
                auth_metadata = EXCLUDED.auth_metadata,
                updated_at = NOW()
            RETURNING id
            """,
            "live-cert-agent",
            AuthManager.hash_api_key(api_key),
            ["live_cert"],
            _json({"seed": "live-cert"}),
            pg_username,
            hash_password(pg_password),
            _json({"seed": "live-cert"}),
        )
    )


async def _grant(conn: Any, identity_id: int, source_id: str, role_id: int) -> None:
    await conn.execute(
        """
        INSERT INTO identity_source_role_grants
            (identity_id, source_id, role_id, metadata)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (identity_id, source_id, role_id) DO UPDATE
        SET enabled = TRUE,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        identity_id,
        source_id,
        role_id,
        _json({"seed": "live-cert"}),
    )


async def register_live_sources(
    creds: LiveCredentials, results: list[Result], redactor: Redactor
) -> tuple[str, dict[str, str]]:
    conn = await _connect_control()
    api_key = "ag-live-cert-local-api-key"
    pg_username = "live_cert_pg_user"
    pg_password = "live-cert-pg-password"
    source_ids = {
        "postgres": "live_cert_postgres",
        "mysql": "live_cert_mysql",
        "s3": "live_cert_s3",
        "slack": "live_cert_slack",
    }
    try:
        identity_id = await _upsert_identity(
            conn,
            api_key=api_key,
            pg_username=pg_username,
            pg_password=pg_password,
        )
        source_rows: list[tuple[str, str, str, dict[str, Any], dict[str, Any], str]] = []
        if creds.postgres.get("host"):
            pg_user = "generic_read_user"
            pg_password = creds.postgres["users"][pg_user]
            pg_dsn = (
                f"postgresql://{quote(pg_user)}:{quote(pg_password)}"
                f"@{creds.postgres['host']}:{creds.postgres.get('port')}/{creds.postgres['database']}"
                "?sslmode=require"
            )
            source_rows.append(
                (
                    source_ids["postgres"],
                    "Live Cert PostgreSQL",
                    "postgresql",
                    {
                        "connection_string": pg_dsn,
                        "host": creds.postgres["host"],
                        "port": creds.postgres.get("port"),
                        "database": creds.postgres["database"],
                        "user": pg_user,
                        "password": pg_password,
                    },
                    {"seed": "live-cert", "connector_key": "postgresql"},
                    "deterministic_first",
                )
            )
        if creds.mysql.get("host"):
            source_rows.append(
                (
                    source_ids["mysql"],
                    "Live Cert MySQL",
                    "mysql",
                    {
                        "host": creds.mysql["host"],
                        "port": creds.mysql.get("port"),
                        "database": creds.mysql["database"],
                        "user": "generic_read_user",
                        "password": creds.mysql["users"]["generic_read_user"],
                        "ssl": True,
                        "ssl_verify": False,
                    },
                    {"seed": "live-cert", **source_metadata_for_connector("mysql")},
                    "deterministic_first",
                )
            )
        if creds.s3.get("bucket"):
            source_rows.append(
                (
                    source_ids["s3"],
                    "Live Cert S3",
                    "s3",
                    {
                        "bucket": creds.s3["bucket"],
                        "prefix": creds.s3.get("prefix") or "",
                        "region_name": creds.s3.get("region_name") or "us-east-1",
                        "aws_access_key_id": creds.s3.get("aws_access_key_id"),
                        "aws_secret_access_key": creds.s3.get("aws_secret_access_key"),
                    },
                    {"seed": "live-cert", **source_metadata_for_connector("s3")},
                    "bypass",
                )
            )
        if creds.slack.get("bot_token"):
            source_rows.append(
                (
                    source_ids["slack"],
                    "Live Cert Slack",
                    "collaboration",
                    {
                        "workspace": creds.slack.get("channel_name") or "live",
                        "bot_token": creds.slack["bot_token"],
                        "channel_ids": creds.slack.get("channel_id"),
                        "include_messages": True,
                        "include_threads": True,
                        "include_files": True,
                        "max_channels": "1",
                        "max_messages_per_channel": "10",
                        "max_replies_per_thread": "10",
                        "max_files": "10",
                    },
                    {"seed": "live-cert", **source_metadata_for_connector("slack")},
                    "bypass",
                )
            )

        for source_id, name, source_type, config, metadata, cache_strategy in source_rows:
            await conn.execute(
                """
                INSERT INTO data_sources
                    (source_id, name, source_type, connection_config, cache_strategy, enabled, metadata)
                VALUES ($1, $2, $3, $4::jsonb, $5, TRUE, $6::jsonb)
                ON CONFLICT (source_id) DO UPDATE
                SET name = EXCLUDED.name,
                    source_type = EXCLUDED.source_type,
                    connection_config = EXCLUDED.connection_config,
                    cache_strategy = EXCLUDED.cache_strategy,
                    enabled = TRUE,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                source_id,
                name,
                source_type,
                _json(config),
                cache_strategy,
                _json(metadata),
            )

        roles = {
            "postgres": await _upsert_role(
                conn,
                source_id=source_ids["postgres"],
                role_key="live_pg_reader",
                connector_key="postgresql",
                permissions=[
                    {
                        "effect": "allow",
                        "action": "db.table.select",
                        "resource_type": "db.table",
                        "resource_pattern": "*.*",
                    },
                    {
                        "effect": "allow",
                        "action": "db.table.describe",
                        "resource_type": "db.table",
                        "resource_pattern": "*.*",
                    },
                    {
                        "effect": "allow",
                        "action": "discovery.search",
                        "resource_type": "discovery.asset",
                        "resource_pattern": "*",
                    },
                ],
            ),
            "mysql": await _upsert_role(
                conn,
                source_id=source_ids["mysql"],
                role_key="live_mysql_reader",
                connector_key="mysql",
                permissions=[
                    {
                        "effect": "allow",
                        "action": "db.schema.list",
                        "resource_type": "db.schema",
                        "resource_pattern": "*",
                    },
                    {
                        "effect": "allow",
                        "action": "db.table.describe",
                        "resource_type": "db.table",
                        "resource_pattern": "*",
                    },
                    {
                        "effect": "allow",
                        "action": "db.table.select",
                        "resource_type": "db.table",
                        "resource_pattern": "*",
                    },
                    {
                        "effect": "allow",
                        "action": "discovery.search",
                        "resource_type": "discovery.asset",
                        "resource_pattern": "*",
                    },
                    {
                        "effect": "allow",
                        "action": "discovery.asset.read",
                        "resource_type": "discovery.asset",
                        "resource_pattern": "*",
                    },
                ],
            ),
            "s3": await _upsert_role(
                conn,
                source_id=source_ids["s3"],
                role_key="reader",
                connector_key="s3",
            ),
            "slack": await _upsert_role(
                conn,
                source_id=source_ids["slack"],
                role_key="knowledge_reader",
                connector_key="slack",
            ),
        }
        for key, role_id in roles.items():
            await _grant(conn, identity_id, source_ids[key], role_id)

        await conn.execute("DELETE FROM policy_rules WHERE name LIKE 'live-cert-%'")
        await conn.execute(
            """
            INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
            VALUES ($1, 5, $2::jsonb, $3::jsonb, TRUE)
            """,
            "live-cert-allow",
            _json(
                {
                    "source_ids": list(source_ids.values()),
                    "roles": ["live_pg_reader", "live_mysql_reader", "reader", "knowledge_reader"],
                    "operation_types": ["read", "discovery"],
                    "role_semantics": "source_role_keys",
                }
            ),
            _json({"effect": "allow", "redact_columns": ["ssn", "email"]}),
        )
        await conn.execute(
            """
            INSERT INTO cache_policies (source_id, strategy_override, enabled, semantic_enabled)
            SELECT unnest($1::text[]), 'bypass', TRUE, FALSE
            ON CONFLICT (source_id) DO UPDATE
            SET strategy_override = EXCLUDED.strategy_override,
                enabled = EXCLUDED.enabled,
                semantic_enabled = EXCLUDED.semantic_enabled
            """,
            list(source_ids.values()),
        )
        await conn.execute(
            "SELECT pg_notify('onyx_config_changed', $1)",
            _json({"source_ids": list(source_ids.values())}),
        )
        await conn.execute(
            "SELECT pg_notify('onyx_policy_changed', $1)",
            _json({"source_ids": list(source_ids.values())}),
        )
        await _upsert_live_discovery_assets(conn, source_ids)
        add(
            results,
            redactor,
            "Registered Sources",
            "control-plane registration and grants",
            "PASS",
            source_ids=source_ids,
            identity_id=identity_id,
        )
    except Exception as exc:
        add(
            results,
            redactor,
            "Registered Sources",
            "control-plane registration and grants",
            "FAIL",
            error=str(exc),
        )
    finally:
        await conn.close()
    await asyncio.sleep(2)
    return api_key, source_ids


async def _upsert_live_discovery_assets(conn: Any, source_ids: dict[str, str]) -> None:
    assets = [
        (
            source_ids["s3"],
            "document",
            "s3://<live-s3-bucket>/<live-prefix>/",
            "Live S3 Certification Prefix",
            "Live registered S3 certification asset for Gateway discovery, role filtering, policy evaluation, and audit.",
        ),
        (
            source_ids["slack"],
            "message",
            "slack://channel/<live-slack-channel-id>",
            "Live Slack Certification Channel",
            "Live registered Slack certification asset for Gateway discovery, source-role filtering, policy evaluation, and audit.",
        ),
    ]
    for source_id, asset_type, asset_path, title, text in assets:
        await conn.execute(
            """
            INSERT INTO discovery_assets
                (source_id, asset_type, asset_path, title, summary, topics,
                 search_vector, quality_score, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::text[],
                    to_tsvector('english', $7), 0.9, $8::jsonb)
            ON CONFLICT (source_id, asset_type, asset_path) DO UPDATE
            SET title = EXCLUDED.title,
                summary = EXCLUDED.summary,
                topics = EXCLUDED.topics,
                search_vector = EXCLUDED.search_vector,
                quality_score = EXCLUDED.quality_score,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            source_id,
            asset_type,
            asset_path,
            title,
            _json({"summary": text, "doc_type": asset_type, "confidence": 0.9}),
            ["live-cert", source_id],
            f"{title} {text}",
            _json({"seed": "live-cert"}),
        )


def _mcp_call(
    api_key: str, name: str, arguments: dict[str, Any], timeout: float = 30
) -> httpx.Response:
    with httpx.Client(base_url=GATEWAY_URL, timeout=timeout) as client:
        return client.post(
            "/mcp/tools/call",
            json={"name": name, "arguments": arguments},
            headers={"Authorization": f"Bearer {api_key}"},
        )


async def _latest_audit(source_id: str) -> dict[str, Any] | None:
    conn = await _connect_control()
    try:
        row = await conn.fetchrow(
            """
            SELECT id, source_id, protocol, status, operation, policy_decision,
                   request_metadata, created_at
            FROM audit_log
            WHERE source_id = $1
            ORDER BY id DESC
            LIMIT 1
            """,
            source_id,
        )
        return dict(row) if row else None
    finally:
        await conn.close()


async def certify_registered_paths(
    creds: LiveCredentials,
    api_key: str,
    source_ids: dict[str, str],
    results: list[Result],
    redactor: Redactor,
) -> None:
    for url, system in ((f"{ADMIN_URL}/health", "Admin"), (f"{GATEWAY_URL}/health", "Gateway")):
        try:
            response = httpx.get(url, timeout=10)
            add(
                results,
                redactor,
                "Registered Stack",
                f"{system} health",
                "PASS" if response.status_code == 200 else "FAIL",
                status_code=response.status_code,
                body=response.text[:200],
            )
        except Exception as exc:
            add(results, redactor, "Registered Stack", f"{system} health", "FAIL", error=str(exc))

    response = _mcp_call(api_key, "agentgate_list_sources", {})
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    visible = json.dumps(body, default=str)
    add(
        results,
        redactor,
        "MCP",
        "list_sources includes registered live sources",
        (
            "PASS"
            if response.status_code == 200 and all(v in visible for v in source_ids.values())
            else "FAIL"
        ),
        status_code=response.status_code,
        source_ids=source_ids,
    )

    pg = _mcp_call(
        api_key,
        "agentgate_query",
        {
            "source_id": source_ids["postgres"],
            "sql": "SELECT current_database() AS database_name, current_user AS db_user",
        },
    )
    add(
        results,
        redactor,
        "MCP",
        "registered live PostgreSQL query",
        "PASS" if pg.status_code == 200 and "generic_read_user" in pg.text else "FAIL",
        status_code=pg.status_code,
        body=pg.text[:500],
    )

    mysql = _mcp_call(
        api_key,
        "agentgate_query",
        {
            "source_id": source_ids["mysql"],
            "sql": "SELECT table_name FROM information_schema.tables LIMIT 1",
        },
    )
    add(
        results,
        redactor,
        "MCP",
        "registered live MySQL query",
        "PASS" if mysql.status_code == 200 else "FAIL",
        status_code=mysql.status_code,
        body=mysql.text[:500],
    )

    describe_pg = _mcp_call(
        api_key, "agentgate_describe_source", {"source_id": source_ids["postgres"]}
    )
    add(
        results,
        redactor,
        "MCP",
        "describe registered PostgreSQL source",
        "PASS" if describe_pg.status_code == 200 else "FAIL",
        status_code=describe_pg.status_code,
        body=describe_pg.text[:500],
    )

    for key in ("s3", "slack"):
        discover = _mcp_call(
            api_key,
            "agentgate_discover",
            {"source_id": source_ids[key], "query": "live certification", "limit": 5},
        )
        add(
            results,
            redactor,
            "MCP",
            f"discover registered live {key} source",
            "PASS" if discover.status_code == 200 and source_ids[key] in discover.text else "FAIL",
            status_code=discover.status_code,
            body=discover.text[:500],
        )

    adapter_checks = [
        (
            "s3",
            "S3",
            get_adapter("s3", {"connector_key": "s3"}),
            {
                "bucket": creds.s3.get("bucket"),
                "prefix": creds.s3.get("prefix") or "",
                "region_name": creds.s3.get("region_name") or "us-east-1",
                "aws_access_key_id": creds.s3.get("aws_access_key_id"),
                "aws_secret_access_key": creds.s3.get("aws_secret_access_key"),
            },
        ),
        (
            "slack",
            "Slack",
            get_adapter("slack", {"connector_key": "slack"}),
            {
                "workspace": creds.slack.get("channel_name") or "live",
                "bot_token": creds.slack.get("bot_token"),
                "channel_ids": creds.slack.get("channel_id"),
                "include_messages": True,
                "include_threads": True,
                "include_files": True,
                "max_channels": "1",
                "max_messages_per_channel": "10",
                "max_replies_per_thread": "10",
                "max_files": "10",
            },
        ),
    ]
    for key, label, adapter, cfg in adapter_checks:
        try:
            status = await adapter.probe(cfg, timeout_seconds=20)
            add(
                results,
                redactor,
                "Registered Connector",
                f"{label} registered config probe",
                "PASS" if status.healthy else "FAIL",
                latency_ms=round(status.latency_ms, 2),
                error=status.error,
            )
            assets = await adapter.list_assets(cfg)
            add(
                results,
                redactor,
                "Registered Connector",
                f"{label} registered config list assets",
                "PASS" if assets else "FAIL",
                asset_count=len(assets),
                first_asset=(assets[0].get("asset_path") if assets else None),
            )
        except Exception as exc:
            add(
                results,
                redactor,
                "Registered Connector",
                f"{label} registered config probe/list",
                "FAIL",
                error=str(exc),
            )

    for key, source_id in source_ids.items():
        row = await _latest_audit(source_id)
        add(
            results,
            redactor,
            "Audit",
            f"{key} latest registered-source audit row",
            "PASS" if row else "WARN",
            audit_id=(row or {}).get("id"),
            audit_status=(row or {}).get("status"),
            protocol=(row or {}).get("protocol"),
        )


def write_report(
    path: Path,
    creds: LiveCredentials,
    results: list[Result],
    redactor: Redactor,
    started: datetime,
    finished: datetime,
) -> None:
    # Certification evidence is generated into build/ (gitignored) rather than
    # committed, so the output directory may not exist yet.
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    lines = [
        "# Registered Live-Source Certification Report",
        "",
        f"- Started: `{started.isoformat()}`",
        f"- Finished: `{finished.isoformat()}`",
        "- Runner: `tools/live_registered_sources_certify.py`",
        "- Credential file: `<external credential file supplied at runtime>`",
        f"- Result counts: {', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}",
        "",
        "## Scope",
        "",
        "- Registers live PostgreSQL, MySQL, S3, and Slack sources in the local Onyx control plane.",
        "- Creates source roles, identity grants, allow policy, cache policy, and registry/policy notifications.",
        "- Exercises Gateway/MCP for SQL sources and registered connector probe/list flows for S3 and Slack.",
        "- Checks audit rows for governed registered-source traffic where runtime audit exists.",
        "",
        "## Results",
        "",
        "| Status | System | Check | Details |",
        "|---|---|---|---|",
    ]
    for result in results:
        lines.append(
            f"| `{result.status}` | {result.system} | {result.check} | `{redactor.text(json.dumps(result.details, default=str, sort_keys=True))}` |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Raw credential values were not written to this report.",
            "- Live source connection configs are stored in the local compose control DB for this certification run.",
            "- S3 and Slack runtime query is connector/discovery-oriented in this MVP, so certification uses registered connector probe/list paths plus MCP source visibility.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


async def amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    started = datetime.now(UTC)
    creds = load_credentials(args.credentials_file)
    redactor = Redactor(creds.secrets)
    results: list[Result] = []

    api_key, source_ids = await register_live_sources(creds, results, redactor)
    await certify_registered_paths(creds, api_key, source_ids, results, redactor)

    finished = datetime.now(UTC)
    report = (
        args.report
        or Path("build/certification")
        / f"{finished.date().isoformat()}-registered-live-source-certification.md"
    )
    write_report(report, creds, results, redactor, started, finished)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(f"Report written: {report}")
    print("Summary:", json.dumps(counts, sort_keys=True))
    return 1 if counts.get("FAIL") else 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
