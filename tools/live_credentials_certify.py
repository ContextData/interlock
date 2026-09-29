#!/usr/bin/env python3
"""Live credential certification runner.

This script intentionally reads credentials from a local text file and never
prints raw secret values. It creates disposable artifacts only and writes a
redacted markdown report under build/certification/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUS_ORDER = {"FAIL": 0, "WARN": 1, "SKIP": 2, "PASS": 3}


@dataclass
class Result:
    system: str
    check: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class LiveCredentials:
    postgres: dict[str, Any]
    mysql: dict[str, Any]
    s3: dict[str, Any]
    slack: dict[str, Any]
    secrets: list[str]


class Redactor:
    def __init__(self, secrets: list[str]) -> None:
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._patterns = [
            re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
            re.compile(r"xapp-[A-Za-z0-9-]+"),
            re.compile(r"AKIA[0-9A-Z]{16}"),
            re.compile(r"(?i)(password|token|secret|authorization|cookie)=([^\\s,;]+)"),
        ]

    def text(self, value: Any) -> str:
        text = str(value)
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        for pattern in self._patterns:
            text = pattern.sub(
                lambda m: f"{m.group(1)}=<redacted>" if m.groups() else "<redacted>", text
            )
        return text

    def obj(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if any(word in str(key).lower() for word in ("password", "token", "secret", "key")):
                    redacted[key] = "<redacted>"
                else:
                    redacted[key] = self.obj(item)
            return redacted
        if isinstance(value, list):
            return [self.obj(item) for item in value]
        if isinstance(value, tuple):
            return [self.obj(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _section_name(line: str) -> str | None:
    text = line.strip().lstrip("#").strip().lower()
    if not text:
        return None
    if "postgres" in text:
        return "postgres"
    if "mysql" in text:
        return "mysql"
    if text == "s3" or "bucket" in text:
        return "s3"
    if "slack" in text:
        return "slack"
    return None


def _kv(line: str) -> tuple[str, str] | None:
    if ":" in line:
        key, value = line.split(":", 1)
    elif "=" in line:
        key, value = line.split("=", 1)
    else:
        return None
    return key.strip().lower().replace(" ", "_"), value.strip()


def _parse_db_section(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {"users": {}}
    for line in lines:
        stripped = line.strip()
        user_match = re.match(r"-\s*([A-Za-z0-9_@.-]+)\s*\(\s*password\s*:\s*([^)]*)\)", stripped)
        if user_match:
            data["users"][user_match.group(1)] = user_match.group(2).strip()
            continue
        pair = _kv(stripped)
        if not pair:
            continue
        key, value = pair
        if key in {"db_name", "database", "database_name"}:
            data["database"] = value
        elif key == "port":
            data["port"] = int(value)
        elif key == "host":
            data["host"] = value
    return data


def _parse_s3_section(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    loose: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        pair = _kv(stripped)
        if pair:
            key, value = pair
            if key == "bucket":
                bucket_match = re.search(r"s3://([^\s)]+)", value)
                name_match = re.search(r"name:\s*([^)]+)", value)
                data["bucket"] = (
                    (name_match or bucket_match).group(1) if (name_match or bucket_match) else value
                )
            elif key == "region":
                data["region_name"] = value
            elif key == "prefix":
                data["prefix"] = value
            continue
        loose.append(stripped)
    if loose:
        data["aws_access_key_id"] = loose[0]
    if len(loose) > 1:
        data["aws_secret_access_key"] = loose[1]
    return data


def _parse_slack_section(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    scopes: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        pair = _kv(stripped)
        if not pair:
            continue
        key, value = pair
        if key == "channel_name":
            data["channel_name"] = value
        elif key == "channel_id":
            data["channel_id"] = value
        elif key == "bot_user_oauth_token":
            data["bot_token"] = value
        elif key == "app-level_tokens":
            data["app_token"] = value
        elif key in {"channels", "chat", "files", "groups", "team", "users"}:
            scopes.append(f"{key}:{value}")
    data["scopes"] = scopes
    return data


def load_credentials(path: Path) -> LiveCredentials:
    raw = path.read_text()
    sections: dict[str, list[str]] = {"s3": [], "postgres": [], "mysql": [], "slack": []}
    current: str | None = None
    for line in raw.splitlines():
        if line.strip().startswith("#"):
            current = _section_name(line)
            continue
        if current:
            sections[current].append(line)

    postgres = _parse_db_section(sections["postgres"])
    mysql = _parse_db_section(sections["mysql"])
    s3 = _parse_s3_section(sections["s3"])
    slack = _parse_slack_section(sections["slack"])

    secrets: list[str] = []
    for data in (postgres, mysql, s3, slack):
        for key, value in data.items():
            if isinstance(value, dict):
                secrets.extend(str(v) for v in value.values())
            elif any(word in key for word in ("password", "token", "secret", "key")):
                secrets.append(str(value))
    return LiveCredentials(postgres=postgres, mysql=mysql, s3=s3, slack=slack, secrets=secrets)


def add(
    results: list[Result], redactor: Redactor, system: str, check: str, status: str, **details: Any
) -> None:
    result = Result(system, check, status, redactor.obj(details))
    results.append(result)
    print(f"[{status}] {system} - {check}: {json.dumps(result.details, default=str)}", flush=True)


async def _pg_connect(cfg: dict[str, Any], user: str):
    import asyncpg

    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(
        host=cfg["host"],
        port=int(cfg.get("port") or 5432),
        database=cfg["database"],
        user=user,
        password=cfg["users"][user],
        ssl=tls,
        timeout=12,
        statement_cache_size=0,
    )


async def _pg_exec_expect(conn: Any, sql: str, should_succeed: bool) -> tuple[bool, str | None]:
    try:
        await conn.execute(sql)
        return should_succeed, None if should_succeed else "operation unexpectedly succeeded"
    except Exception as exc:
        return (not should_succeed), None if not should_succeed else str(exc)


async def certify_postgres(
    creds: LiveCredentials, results: list[Result], redactor: Redactor, stamp: str
) -> None:
    cfg = creds.postgres
    users = cfg.get("users") or {}
    required = {"generic_read_user", "generic_write_user", "generic_ddl_user"}
    if not cfg.get("host") or not required.issubset(users):
        add(
            results,
            redactor,
            "PostgreSQL",
            "credential completeness",
            "SKIP",
            reason="missing host/database/users",
        )
        return

    live_users = [
        user
        for user in ("generic_read_user", "generic_write_user", "generic_ddl_user")
        if user in users
    ]
    conns: dict[str, Any] = {}
    for user in live_users:
        try:
            conn = await _pg_connect(cfg, user)
            conns[user] = conn
            row = await conn.fetchrow(
                "SELECT current_database() AS db, current_user AS current_user, version() AS version"
            )
            add(
                results,
                redactor,
                "PostgreSQL",
                f"{user} connects and runs SELECT 1",
                "PASS",
                database=row["db"],
                current_user=row["current_user"],
                version=str(row["version"]).split(" on ")[0],
            )
            tables = await conn.fetch("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name
                LIMIT 25
                """)
            add(
                results,
                redactor,
                "PostgreSQL",
                f"{user} information_schema visibility",
                "PASS",
                visible_tables=len(tables),
            )
        except Exception as exc:
            add(
                results, redactor, "PostgreSQL", f"{user} connection/select", "FAIL", error=str(exc)
            )

    table = f"public.interlock_live_cert_{stamp}"
    setup = conns.get("generic_ddl_user")
    if setup is None:
        add(
            results,
            redactor,
            "PostgreSQL",
            "disposable table matrix",
            "SKIP",
            reason="ddl user unavailable",
        )
    else:
        try:
            await setup.execute(f"DROP TABLE IF EXISTS {table}")
            await setup.execute(
                f"CREATE TABLE {table} (id integer PRIMARY KEY, note text, updated_at timestamptz DEFAULT now())"
            )
            await setup.execute(f"INSERT INTO {table} (id, note) VALUES (1, 'seed')")
            await setup.execute(f"GRANT SELECT ON {table} TO generic_read_user")
            await setup.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO generic_write_user")
            add(
                results,
                redactor,
                "PostgreSQL",
                "disposable table setup and grants",
                "PASS",
                table=table,
            )

            matrix = [
                (
                    "generic_read_user",
                    f"SELECT count(*) FROM {table}",
                    True,
                    "read user can select disposable table",
                ),
                (
                    "generic_read_user",
                    f"INSERT INTO {table} (id, note) VALUES (10, 'should-fail')",
                    False,
                    "read user cannot insert",
                ),
                ("generic_read_user", f"DROP TABLE {table}", False, "read user cannot drop"),
                (
                    "generic_write_user",
                    f"INSERT INTO {table} (id, note) VALUES (2, 'write')",
                    True,
                    "write user can insert",
                ),
                (
                    "generic_write_user",
                    f"UPDATE {table} SET note = 'updated' WHERE id = 2",
                    True,
                    "write user can update",
                ),
                (
                    "generic_write_user",
                    f"DELETE FROM {table} WHERE id = 2",
                    False,
                    "write user cannot delete rows",
                ),
                ("generic_write_user", f"DROP TABLE {table}", False, "write user cannot drop"),
                (
                    "generic_ddl_user",
                    f"CREATE TABLE public.interlock_live_cert_{stamp}_ddl (id integer); DROP TABLE public.interlock_live_cert_{stamp}_ddl",
                    True,
                    "ddl user can create and drop disposable table",
                ),
            ]
            for user, sql, expected, label in matrix:
                conn = conns.get(user)
                if conn is None:
                    add(
                        results, redactor, "PostgreSQL", label, "SKIP", reason=f"{user} unavailable"
                    )
                    continue
                ok, error = await _pg_exec_expect(conn, sql, expected)
                add(
                    results,
                    redactor,
                    "PostgreSQL",
                    label,
                    "PASS" if ok else "FAIL",
                    expected_success=expected,
                    error=error,
                )
        except Exception as exc:
            add(
                results,
                redactor,
                "PostgreSQL",
                "disposable table setup and grants",
                "FAIL",
                error=str(exc),
                table=table,
            )
        finally:
            try:
                await setup.execute(f"DROP TABLE IF EXISTS public.interlock_live_cert_{stamp}_ddl")
                await setup.execute(f"DROP TABLE IF EXISTS {table}")
                add(
                    results, redactor, "PostgreSQL", "disposable table cleanup", "PASS", table=table
                )
            except Exception as exc:
                add(
                    results,
                    redactor,
                    "PostgreSQL",
                    "disposable table cleanup",
                    "WARN",
                    table=table,
                    error=str(exc),
                )

    for conn in conns.values():
        await conn.close()


async def _mysql_connect(cfg: dict[str, Any], user: str):
    import aiomysql

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return await aiomysql.connect(
        host=cfg["host"],
        port=int(cfg.get("port") or 3306),
        db=cfg["database"],
        user=user,
        password=cfg["users"][user],
        ssl=context,
        autocommit=True,
        connect_timeout=12,
    )


async def _mysql_exec_expect(conn: Any, sql: str, should_succeed: bool) -> tuple[bool, str | None]:
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql)
        return should_succeed, None if should_succeed else "operation unexpectedly succeeded"
    except Exception as exc:
        return (not should_succeed), None if not should_succeed else str(exc)


async def certify_mysql(
    creds: LiveCredentials, results: list[Result], redactor: Redactor, stamp: str
) -> None:
    cfg = creds.mysql
    users = cfg.get("users") or {}
    if not cfg.get("host") or not users:
        add(
            results,
            redactor,
            "MySQL",
            "credential completeness",
            "SKIP",
            reason="missing host/database/users",
        )
        return

    live_users = [
        user
        for user in (
            "generic_read_user",
            "generic_write_user",
            "generic_ddl_user",
            "generic_full_user",
        )
        if user in users
    ]
    conns: dict[str, Any] = {}
    for user in live_users:
        try:
            conn = await _mysql_connect(cfg, user)
            conns[user] = conn
            async with conn.cursor() as cur:
                await cur.execute("SELECT DATABASE(), CURRENT_USER(), VERSION()")
                row = await cur.fetchone()
                await cur.execute("""
                    SELECT table_schema, table_name
                    FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                    ORDER BY table_schema, table_name
                    LIMIT 25
                    """)
                tables = await cur.fetchall()
            add(
                results,
                redactor,
                "MySQL",
                f"{user} connects, selects, and introspects",
                "PASS",
                database=row[0],
                current_user=row[1],
                version=row[2],
                visible_tables=len(tables),
            )
        except Exception as exc:
            add(results, redactor, "MySQL", f"{user} connection/select", "FAIL", error=str(exc))

    setup_user = "generic_full_user" if "generic_full_user" in conns else "generic_ddl_user"
    setup = conns.get(setup_user)
    table = f"interlock_live_cert_{stamp}"
    if setup is None:
        add(
            results,
            redactor,
            "MySQL",
            "disposable table matrix",
            "SKIP",
            reason="full/ddl user unavailable",
        )
    else:
        try:
            async with setup.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS `{table}`")
                await cur.execute(f"CREATE TABLE `{table}` (id INT PRIMARY KEY, note VARCHAR(255))")
                await cur.execute(f"INSERT INTO `{table}` (id, note) VALUES (1, 'seed')")
                grant_errors: list[str] = []
                for grant_sql in (
                    f"GRANT SELECT ON `{cfg['database']}`.`{table}` TO 'generic_read_user'@'%'",
                    f"GRANT SELECT, INSERT, UPDATE ON `{cfg['database']}`.`{table}` TO 'generic_write_user'@'%'",
                    f"GRANT ALL PRIVILEGES ON `{cfg['database']}`.`{table}` TO 'generic_ddl_user'@'%'",
                ):
                    try:
                        await cur.execute(grant_sql)
                    except Exception as exc:
                        grant_errors.append(str(exc))
            add(
                results,
                redactor,
                "MySQL",
                "disposable table setup",
                "PASS" if not grant_errors else "WARN",
                table=table,
                setup_user=setup_user,
                grant_error_count=len(grant_errors),
                grant_errors=grant_errors[:3],
            )
            matrix = [
                (
                    "generic_read_user",
                    f"SELECT COUNT(*) FROM `{table}`",
                    True,
                    "read user can select disposable table",
                ),
                (
                    "generic_read_user",
                    f"INSERT INTO `{table}` (id, note) VALUES (10, 'should-fail')",
                    False,
                    "read user cannot insert",
                ),
                ("generic_read_user", f"DROP TABLE `{table}`", False, "read user cannot drop"),
                (
                    "generic_write_user",
                    f"INSERT INTO `{table}` (id, note) VALUES (2, 'write')",
                    True,
                    "write user can insert",
                ),
                (
                    "generic_write_user",
                    f"UPDATE `{table}` SET note = 'updated'",
                    True,
                    "write user can update disposable table",
                ),
                (
                    "generic_write_user",
                    f"UPDATE `{table}` SET note = 'updated-with-predicate' WHERE id = 2",
                    True,
                    "write user can update with WHERE predicate",
                ),
                (
                    "generic_write_user",
                    f"DELETE FROM `{table}` WHERE id = 2",
                    False,
                    "write user cannot delete rows",
                ),
                ("generic_write_user", f"DROP TABLE `{table}`", False, "write user cannot drop"),
                (
                    "generic_ddl_user",
                    f"CREATE TABLE `interlock_live_cert_{stamp}_ddl` (id INT PRIMARY KEY); DROP TABLE `interlock_live_cert_{stamp}_ddl`",
                    True,
                    "ddl user can create and drop disposable table",
                ),
            ]
            if "generic_full_user" in conns:
                matrix.extend(
                    [
                        (
                            "generic_full_user",
                            f"INSERT INTO `{table}` (id, note) VALUES (3, 'full')",
                            True,
                            "full user can insert",
                        ),
                        (
                            "generic_full_user",
                            f"DELETE FROM `{table}` WHERE id = 3",
                            True,
                            "full user can delete",
                        ),
                    ]
                )
            for user, sql, expected, label in matrix:
                conn = conns.get(user)
                if conn is None:
                    add(results, redactor, "MySQL", label, "SKIP", reason=f"{user} unavailable")
                    continue
                ok, error = await _mysql_exec_expect(conn, sql, expected)
                add(
                    results,
                    redactor,
                    "MySQL",
                    label,
                    "PASS" if ok else "FAIL",
                    expected_success=expected,
                    error=error,
                )
        except Exception as exc:
            add(
                results,
                redactor,
                "MySQL",
                "disposable table setup",
                "FAIL",
                error=str(exc),
                table=table,
            )
        finally:
            try:
                async with setup.cursor() as cur:
                    await cur.execute(f"DROP TABLE IF EXISTS `interlock_live_cert_{stamp}_ddl`")
                    await cur.execute(f"DROP TABLE IF EXISTS `{table}`")
                add(results, redactor, "MySQL", "disposable table cleanup", "PASS", table=table)
            except Exception as exc:
                add(
                    results,
                    redactor,
                    "MySQL",
                    "disposable table cleanup",
                    "WARN",
                    table=table,
                    error=str(exc),
                )

    for conn in conns.values():
        conn.close()


async def certify_onyx_connector_paths(
    creds: LiveCredentials, results: list[Result], redactor: Redactor, stamp: str
) -> None:
    from interlock.connections.connectors import get_adapter
    from interlock.connections.manager import ConnectionManager

    if creds.postgres.get("host") and "generic_read_user" in creds.postgres.get("users", {}):
        cfg = {
            "host": creds.postgres["host"],
            "port": creds.postgres.get("port"),
            "database": creds.postgres["database"],
            "user": "generic_read_user",
            "password": creds.postgres["users"]["generic_read_user"],
            "ssl": ssl.create_default_context(),
        }
        cfg["ssl"].check_hostname = False
        cfg["ssl"].verify_mode = ssl.CERT_NONE
        status = await ConnectionManager.probe_unsaved("postgresql", cfg, timeout_seconds=12)
        add(
            results,
            redactor,
            "Onyx Admin Probe",
            "PostgreSQL unsaved source probe",
            "PASS" if status.healthy else "FAIL",
            latency_ms=round(status.latency_ms, 2),
            error=status.error,
        )

    if creds.mysql.get("host") and "generic_read_user" in creds.mysql.get("users", {}):
        adapter = get_adapter("mysql", {"connector_key": "mysql"})
        cfg = {
            "host": creds.mysql["host"],
            "port": creds.mysql.get("port"),
            "database": creds.mysql["database"],
            "user": "generic_read_user",
            "password": creds.mysql["users"]["generic_read_user"],
            "ssl": True,
            "ssl_verify": False,
        }
        status = await adapter.probe(cfg, timeout_seconds=12)
        add(
            results,
            redactor,
            "Onyx Connector",
            "MySQL adapter probe",
            "PASS" if status.healthy else "FAIL",
            latency_ms=round(status.latency_ms, 2),
            error=status.error,
        )
        try:
            metadata = await adapter.introspect(cfg)
            add(
                results,
                redactor,
                "Onyx Connector",
                "MySQL adapter introspect",
                "PASS",
                tables=len(metadata.get("tables", [])),
            )
        except Exception as exc:
            add(
                results,
                redactor,
                "Onyx Connector",
                "MySQL adapter introspect",
                "FAIL",
                error=str(exc),
            )

    if creds.s3.get("bucket"):
        adapter = get_adapter("s3", {"connector_key": "s3"})
        cfg = {
            "bucket": creds.s3["bucket"],
            "prefix": creds.s3.get("prefix") or "",
            "region_name": creds.s3.get("region_name") or "us-east-1",
            "aws_access_key_id": creds.s3.get("aws_access_key_id"),
            "aws_secret_access_key": creds.s3.get("aws_secret_access_key"),
        }
        status = await adapter.probe(cfg, timeout_seconds=20)
        add(
            results,
            redactor,
            "Onyx Connector",
            "S3 adapter probe",
            "PASS" if status.healthy else "FAIL",
            latency_ms=round(status.latency_ms, 2),
            error=status.error,
        )
        try:
            assets = await adapter.list_assets(cfg)
            add(
                results,
                redactor,
                "Onyx Connector",
                "S3 adapter list assets",
                "PASS",
                asset_count=len(assets),
                sample_paths=[a.get("asset_path") for a in assets[:3]],
            )
        except Exception as exc:
            add(
                results,
                redactor,
                "Onyx Connector",
                "S3 adapter list assets",
                "FAIL",
                error=str(exc),
            )

    if creds.slack.get("bot_token") and creds.slack.get("channel_id"):
        adapter = get_adapter("slack", {"connector_key": "slack"})
        cfg = {
            "workspace": creds.slack.get("channel_name") or "live",
            "bot_token": creds.slack["bot_token"],
            "channel_ids": creds.slack["channel_id"],
            "include_messages": True,
            "include_threads": True,
            "include_files": True,
            "max_channels": "1",
            "max_messages_per_channel": "10",
            "max_replies_per_thread": "10",
            "max_files": "10",
        }
        status = await adapter.probe(cfg, timeout_seconds=12)
        add(
            results,
            redactor,
            "Onyx Connector",
            "Slack adapter probe",
            "PASS" if status.healthy else "FAIL",
            latency_ms=round(status.latency_ms, 2),
            error=status.error,
        )
        try:
            assets = await adapter.list_assets(cfg)
            kinds: dict[str, int] = {}
            for asset in assets:
                kinds[str(asset.get("asset_type"))] = kinds.get(str(asset.get("asset_type")), 0) + 1
            add(
                results,
                redactor,
                "Onyx Connector",
                "Slack adapter list assets",
                "PASS",
                asset_count=len(assets),
                asset_types=kinds,
            )
            if assets:
                payload = await adapter.fetch_asset(cfg, str(assets[0]["asset_path"]))
                add(
                    results,
                    redactor,
                    "Onyx Connector",
                    "Slack adapter fetch first asset",
                    "PASS",
                    asset_ref=assets[0]["asset_path"],
                    bytes=len(payload),
                )
        except Exception as exc:
            add(
                results,
                redactor,
                "Onyx Connector",
                "Slack adapter assets/fetch",
                "FAIL",
                error=str(exc),
            )


async def certify_s3(
    creds: LiveCredentials, results: list[Result], redactor: Redactor, stamp: str
) -> None:
    cfg = creds.s3
    if (
        not cfg.get("bucket")
        or not cfg.get("aws_access_key_id")
        or not cfg.get("aws_secret_access_key")
    ):
        add(
            results,
            redactor,
            "S3",
            "credential completeness",
            "SKIP",
            reason="missing bucket/key/secret",
        )
        return
    try:
        from aiobotocore.session import get_session
    except ImportError as exc:
        add(results, redactor, "S3", "dependency availability", "FAIL", error=str(exc))
        return
    bucket = cfg["bucket"]
    prefix = cfg.get("prefix") or ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    key = f"{prefix}interlock-live-cert/{stamp}/certification.txt"
    body = f"InterLock live S3 certification {stamp}\\n".encode()
    session = get_session()
    async with session.create_client(
        "s3",
        region_name=cfg.get("region_name") or "us-east-1",
        aws_access_key_id=cfg["aws_access_key_id"],
        aws_secret_access_key=cfg["aws_secret_access_key"],
    ) as client:
        try:
            listing = await client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=10)
            add(
                results,
                redactor,
                "S3",
                "list provided prefix",
                "PASS",
                bucket=bucket,
                prefix=prefix,
                object_count=listing.get("KeyCount", 0),
            )
        except Exception as exc:
            add(
                results,
                redactor,
                "S3",
                "list provided prefix",
                "FAIL",
                bucket=bucket,
                prefix=prefix,
                error=str(exc),
            )
            return
        try:
            put = await client.put_object(
                Bucket=bucket, Key=key, Body=body, ContentType="text/plain"
            )
            add(
                results,
                redactor,
                "S3",
                "write disposable object",
                "PASS",
                key=key,
                etag=put.get("ETag"),
            )
            fetched = await client.get_object(Bucket=bucket, Key=key)
            async with fetched["Body"] as stream:
                data = await stream.read()
            add(
                results,
                redactor,
                "S3",
                "read disposable object",
                "PASS" if data == body else "FAIL",
                key=key,
                bytes=len(data),
            )
            await client.delete_object(Bucket=bucket, Key=key)
            try:
                await client.get_object(Bucket=bucket, Key=key)
                add(
                    results,
                    redactor,
                    "S3",
                    "delete disposable object verification",
                    "FAIL",
                    key=key,
                    error="object still readable",
                )
            except Exception:
                add(
                    results,
                    redactor,
                    "S3",
                    "delete disposable object verification",
                    "PASS",
                    key=key,
                )
        except Exception as exc:
            add(
                results,
                redactor,
                "S3",
                "write/read/delete disposable object",
                "FAIL",
                key=key,
                error=str(exc),
            )
            try:
                await client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass


async def certify_slack(
    creds: LiveCredentials, results: list[Result], redactor: Redactor, stamp: str
) -> None:
    cfg = creds.slack
    if not cfg.get("bot_token") or not cfg.get("channel_id"):
        add(
            results,
            redactor,
            "Slack",
            "credential completeness",
            "SKIP",
            reason="missing bot token/channel id",
        )
        return
    try:
        from slack_sdk.errors import SlackApiError
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError as exc:
        add(results, redactor, "Slack", "dependency availability", "FAIL", error=str(exc))
        return

    client = AsyncWebClient(token=cfg["bot_token"])
    channel_id = cfg["channel_id"]
    message_ts: str | None = None
    reply_ts: str | None = None
    file_id: str | None = None
    try:
        auth = await client.auth_test()
        add(
            results,
            redactor,
            "Slack",
            "auth.test",
            "PASS",
            team=auth.get("team"),
            user=auth.get("user"),
            bot_id=auth.get("bot_id"),
        )
        info = await client.conversations_info(channel=channel_id)
        channel = info.get("channel", {})
        add(
            results,
            redactor,
            "Slack",
            "channel info",
            "PASS",
            channel_id=channel_id,
            channel_name=channel.get("name"),
            is_member=channel.get("is_member"),
        )
        history = await client.conversations_history(channel=channel_id, limit=10)
        add(
            results,
            redactor,
            "Slack",
            "message history read",
            "PASS",
            message_count=len(history.get("messages", [])),
        )

        posted = await client.chat_postMessage(
            channel=channel_id,
            text=f"InterLock live certification message {stamp}",
        )
        message_ts = posted.get("ts")
        add(
            results,
            redactor,
            "Slack",
            "post disposable message",
            "PASS",
            channel_id=channel_id,
            ts=message_ts,
        )
        reply = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"InterLock live certification thread reply {stamp}",
        )
        reply_ts = reply.get("ts")
        add(
            results,
            redactor,
            "Slack",
            "post disposable thread reply",
            "PASS",
            channel_id=channel_id,
            thread_ts=message_ts,
            ts=reply_ts,
        )
        replies = await client.conversations_replies(channel=channel_id, ts=message_ts, limit=10)
        add(
            results,
            redactor,
            "Slack",
            "thread replies readback",
            "PASS",
            reply_count=len(replies.get("messages", [])),
        )

        upload = await client.files_upload_v2(
            channel=channel_id,
            filename=f"interlock-live-cert-{stamp}.txt",
            title=f"InterLock live certification {stamp}",
            content=f"InterLock live Slack file certification {stamp}\\n",
            initial_comment=f"InterLock live certification file {stamp}",
        )
        uploaded_files = upload.get("files") or []
        file_id = (uploaded_files[0] if uploaded_files else upload.get("file") or {}).get("id")
        add(
            results,
            redactor,
            "Slack",
            "upload disposable file",
            "PASS" if file_id else "WARN",
            file_id=file_id,
            file_count=len(uploaded_files),
        )
        if file_id:
            file_info = await client.files_info(file=file_id)
            add(
                results,
                redactor,
                "Slack",
                "file info readback",
                "PASS",
                file_id=file_id,
                title=(file_info.get("file") or {}).get("title"),
            )
    except SlackApiError as exc:
        add(results, redactor, "Slack", "live API flow", "FAIL", error=exc.response.get("error"))
    except Exception as exc:
        add(results, redactor, "Slack", "live API flow", "FAIL", error=str(exc))
    finally:
        if file_id:
            try:
                await client.files_delete(file=file_id)
                add(results, redactor, "Slack", "delete disposable file", "PASS", file_id=file_id)
            except Exception as exc:
                add(
                    results,
                    redactor,
                    "Slack",
                    "delete disposable file",
                    "WARN",
                    file_id=file_id,
                    error=str(exc),
                )
        for ts, label in (
            (reply_ts, "delete disposable thread reply"),
            (message_ts, "delete disposable message"),
        ):
            if not ts:
                continue
            try:
                await client.chat_delete(channel=channel_id, ts=ts)
                add(results, redactor, "Slack", label, "PASS", ts=ts)
            except Exception as exc:
                add(results, redactor, "Slack", label, "WARN", ts=ts, error=str(exc))


def write_report(
    path: Path,
    creds: LiveCredentials,
    results: list[Result],
    redactor: Redactor,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    systems = sorted({result.system for result in results})
    lines = [
        "# Live Credentials Certification Report",
        "",
        f"- Started: `{started_at.isoformat()}`",
        f"- Finished: `{finished_at.isoformat()}`",
        "- Credential file: `<external credential file supplied at runtime>`",
        f"- Systems tested: {', '.join(systems) if systems else 'none'}",
        f"- Result counts: {', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}",
        "",
        "## Credential Coverage",
        "",
        "| System | Non-secret target | Credential aliases tested |",
        "|---|---|---|",
        f"| PostgreSQL | `{creds.postgres.get('host', '<missing>')}:{creds.postgres.get('port', '<missing>')}/{creds.postgres.get('database', '<missing>')}` | `{', '.join(sorted(creds.postgres.get('users', {}).keys()))}` |",
        f"| MySQL | `{creds.mysql.get('host', '<missing>')}:{creds.mysql.get('port', '<missing>')}/{creds.mysql.get('database', '<missing>')}` | `{', '.join(sorted(creds.mysql.get('users', {}).keys()))}` |",
        f"| S3 | `s3://{creds.s3.get('bucket', '<missing>')}/{creds.s3.get('prefix', '')}` | `access-key + secret-key` |",
        f"| Slack | `{creds.slack.get('channel_name', '<missing>')}` / `{creds.slack.get('channel_id', '<missing>')}` | `bot token` |",
        "",
        "## Results",
        "",
        "| Status | System | Check | Details |",
        "|---|---|---|---|",
    ]
    for result in results:
        details = redactor.text(json.dumps(result.details, default=str, sort_keys=True))
        lines.append(f"| `{result.status}` | {result.system} | {result.check} | `{details}` |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Raw credential values were not written to this report.",
            "- SQL mutation checks used disposable `interlock_live_cert_*` tables and attempted cleanup.",
            "- S3 mutation checks used a disposable object under the provided prefix and attempted cleanup.",
            "- Slack mutation checks used disposable messages/files in the provided channel and attempted cleanup; any cleanup warning above should be manually reviewed in Slack.",
        ]
    )
    path.write_text("\n".join(lines) + "\n")


async def amain() -> int:
    parser = argparse.ArgumentParser(
        description="Run live credential certification without printing secrets."
    )
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%d%H%M%S")
    creds = load_credentials(args.credentials_file)
    redactor = Redactor(creds.secrets)
    results: list[Result] = []

    print("Live certification starting. Raw secrets will not be printed.", flush=True)
    await certify_postgres(creds, results, redactor, stamp)
    await certify_mysql(creds, results, redactor, stamp)
    await certify_s3(creds, results, redactor, stamp)
    await certify_slack(creds, results, redactor, stamp)
    await certify_onyx_connector_paths(creds, results, redactor, stamp)

    finished = datetime.now(UTC)
    report_path = (
        args.report
        or Path("build/certification")
        / f"{finished.date().isoformat()}-live-credentials-certification.md"
    )
    write_report(report_path, creds, results, redactor, started, finished)

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(f"Report written: {report_path}", flush=True)
    print("Summary:", json.dumps(counts, sort_keys=True), flush=True)
    return 1 if counts.get("FAIL") else 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(amain()))
    except KeyboardInterrupt:
        # 128 + SIGINT, the conventional shell exit code for Ctrl-C.
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
