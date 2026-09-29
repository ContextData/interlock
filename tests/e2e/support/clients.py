"""Client and polling helpers for compose-backed E2E tests."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx

from tests.e2e.support.config import E2EConfig


@dataclass
class AdminSession:
    client: httpx.Client
    csrf_token: str


async def connect_control_db(config: E2EConfig) -> asyncpg.Connection:
    return await asyncpg.connect(config.control_dsn)


async def connect_source_db(config: E2EConfig) -> asyncpg.Connection:
    return await asyncpg.connect(config.source_dsn)


def agent_headers(config: E2EConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.agent_api_key}"}


def denied_headers(config: E2EConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.denied_api_key}"}


def login_admin(config: E2EConfig) -> AdminSession:
    client = httpx.Client(base_url=config.admin_url, follow_redirects=False, timeout=10)
    response = client.post(
        "/auth/login",
        data={"username": config.admin_username, "password": config.admin_password},
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    csrf = client.get("/auth/csrf")
    csrf.raise_for_status()
    return AdminSession(client=client, csrf_token=str(csrf.json()["csrf"]))


def mcp_call(
    config: E2EConfig,
    name: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    with httpx.Client(base_url=config.gateway_url, timeout=15) as client:
        return client.post(
            "/mcp/tools/call",
            json={"name": name, "arguments": arguments},
            headers=agent_headers(config) if headers is None else headers,
        )


def http_proxy_request(
    config: E2EConfig,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    with httpx.Client(base_url=config.gateway_url, timeout=15) as client:
        return client.request(
            method,
            f"/proxy/{config.source_id_http}/{path.lstrip('/')}",
            headers=agent_headers(config) if headers is None else headers,
            json=json_body,
        )


def upstream_calls(config: E2EConfig) -> dict[str, Any]:
    with httpx.Client(base_url=config.http_upstream_url, timeout=5) as client:
        response = client.get("/calls")
        response.raise_for_status()
        return response.json()


async def wait_for(
    probe: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float = 10.0,
    interval_seconds: float = 0.25,
) -> Any:
    """Poll an async probe until it returns a truthy value or timeout expires."""
    deadline = time.monotonic() + timeout_seconds
    last: Any = None
    while time.monotonic() < deadline:
        last = await probe()
        if last:
            return last
        await asyncio.sleep(interval_seconds)
    return last


async def latest_audit(
    conn: asyncpg.Connection,
    *,
    source_id: str | None = None,
    protocol: str | None = None,
    status: str | None = None,
) -> asyncpg.Record | None:
    clauses: list[str] = []
    args: list[Any] = []
    if source_id is not None:
        args.append(source_id)
        clauses.append(f"source_id = ${len(args)}")
    if status is not None:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await conn.fetch(
        f"""
        SELECT id, identity_id, source_id, operation, status, cache_hit,
               cache_tier, pii_detected, pii_types, request_metadata, created_at
        FROM audit_log
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT 25
        """,
        *args,
    )
    if protocol is None:
        return rows[0] if rows else None
    for row in rows:
        metadata = _decode_jsonish(row["request_metadata"])
        if metadata.get("protocol") == protocol:
            return row
    return None


async def latest_approval(
    conn: asyncpg.Connection,
    *,
    source_id: str,
    status: str | None = None,
) -> asyncpg.Record | None:
    args: list[Any] = [source_id]
    status_clause = ""
    if status is not None:
        args.append(status)
        status_clause = f"AND status = ${len(args)}"
    return await conn.fetchrow(
        f"""
        SELECT id, identity_id, source_id, sql_text, risk_level, status,
               approved_by, request_metadata, created_at
        FROM write_approval_queue
        WHERE source_id = $1
        {status_clause}
        ORDER BY created_at DESC
        LIMIT 1
        """,
        *args,
    )


def _decode_jsonish(value: Any) -> dict[str, Any]:
    """Decode JSONB values that may arrive as dict, JSON text, or nested text."""
    current = value
    for _ in range(3):
        if current is None:
            return {}
        if isinstance(current, dict):
            return current
        if isinstance(current, str):
            current = json.loads(current)
            continue
        return dict(current)
    return {}
