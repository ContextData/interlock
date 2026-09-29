"""Ground-truth readers for the upstreams InterLock governs.

Every reader here answers one question: *what does the system that owns this
data actually contain right now?* None of them route through the gateway, and
none of them read a status column that the gateway wrote.

This exists because the Write Safety defect passed a full e2e suite. The test
covering the exact broken operation asserted only::

    assert response.status_code == 202
    assert "Write queued for approval" in response.text

That is true, and useless: it stops at the moment the system says something
reassuring. The write was never executed, the upstream row survived, and the
console reported a completed write. A test may not conclude that a write
happened because the gateway said so - it has to go and look.

Use these in preference to re-reading through the proxy. Reading back through
the gateway does detect a *missing* write, but it cannot distinguish "the write
happened" from "the read is served from a cache that never saw the write",
which is the distinction several of the audit's cache and write-barrier checks
turn on.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import httpx

from tests.e2e.support.config import E2EConfig

__all__ = [
    "http_upstream_calls",
    "mysql_rows",
    "object_exists",
    "opensearch_document_count",
    "postgres_rows",
    "qdrant_point_count",
    "s3_object_keys",
]


# --------------------------------------------------------------------------
# Relational upstreams
# --------------------------------------------------------------------------


async def postgres_rows(config: E2EConfig, query: str, *args: Any) -> list[dict[str, Any]]:
    """Read directly from the upstream PostgreSQL source, bypassing the proxy."""
    conn = await asyncpg.connect(config.source_dsn)
    try:
        rows = await conn.fetch(query, *args)
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def mysql_rows(config: E2EConfig, query: str, *args: Any) -> list[dict[str, Any]]:
    """Read directly from the upstream MySQL source, bypassing the proxy.

    Addresses MySQL on the host-published port. The stored source config uses
    the compose hostname, which only resolves inside the network.
    """
    import aiomysql

    conn = await aiomysql.connect(
        host=config.source_mysql_host,
        port=config.source_mysql_port,
        user=config.source_mysql_user,
        password=config.source_mysql_password,
        db=config.source_mysql_database,
    )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, args or None)
            return list(await cur.fetchall())
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Object storage
# --------------------------------------------------------------------------


def _s3_client(config: E2EConfig) -> Any:
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=config.s3_url,
        aws_access_key_id=config.s3_access_key,
        aws_secret_access_key=config.s3_secret_key,
        region_name="us-east-1",
    )


def s3_object_keys(config: E2EConfig, bucket: str, prefix: str = "") -> list[str]:
    """List what the object store actually holds."""
    client = _s3_client(config)
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(item["Key"] for item in page.get("Contents", []))
    return sorted(keys)


def object_exists(config: E2EConfig, bucket: str, key: str) -> bool:
    """Whether one object is really present.

    A deletion that the gateway reports as successful but never performed is
    the object-storage analogue of the Write Safety defect.
    """
    from botocore.exceptions import ClientError

    try:
        _s3_client(config).head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


# --------------------------------------------------------------------------
# Search and vector upstreams
# --------------------------------------------------------------------------


def opensearch_document_count(config: E2EConfig, index: str) -> int:
    """Document count straight from OpenSearch."""
    with httpx.Client(base_url=config.opensearch_url, timeout=10) as client:
        response = client.get(f"/{index}/_count")
        response.raise_for_status()
        return int(response.json()["count"])


def qdrant_point_count(config: E2EConfig, collection: str) -> int:
    """Point count straight from Qdrant."""
    with httpx.Client(base_url=config.qdrant_url, timeout=10) as client:
        response = client.get(f"/collections/{collection}")
        response.raise_for_status()
        return int(response.json()["result"]["points_count"])


# --------------------------------------------------------------------------
# HTTP origin
# --------------------------------------------------------------------------


def http_upstream_calls(
    config: E2EConfig, *, method: str | None = None, path: str | None = None
) -> list[dict[str, Any]]:
    """Requests the HTTP origin actually received.

    This is the only way to tell an HTTP write that reached the origin from one
    the gateway merely reported as successful: the origin records every call it
    is given, so an absent entry is proof the request never arrived.
    """
    with httpx.Client(base_url=config.http_upstream_url, timeout=10) as client:
        response = client.get("/calls")
        response.raise_for_status()
        payload = response.json()

    calls = payload.get("calls", payload) if isinstance(payload, dict) else payload
    if not isinstance(calls, list):
        return []

    def matches(call: dict[str, Any]) -> bool:
        if method is not None and str(call.get("method", "")).upper() != method.upper():
            return False
        if path is not None and path not in str(call.get("path", "")):
            return False
        return True

    return [call for call in calls if isinstance(call, dict) and matches(call)]
