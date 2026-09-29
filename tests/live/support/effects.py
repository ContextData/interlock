"""Ground-truth readers for the live upstreams.

Same contract as `tests/e2e/support/effects.py`, against real systems: every
reader answers *what does the system that owns this data actually contain
right now?*, and none of them routes through InterLock. A live certification
that concluded a write succeeded because the gateway returned 200 would
reproduce, against production systems, the exact defect the audit was
commissioned to fix.

These readers connect with the credentials from the credential file directly,
which is deliberate. They are the independent observer, so they must not share
the code path being observed.
"""

from __future__ import annotations

import ssl
from typing import Any

import asyncpg

from tests.live.support.config import LiveConfig

__all__ = [
    "drive_file_names",
    "mysql_rows",
    "object_exists",
    "postgres_rows",
    "s3_object_keys",
    "slack_message_texts",
]


def _managed_tls() -> ssl.SSLContext:
    """TLS for a managed database endpoint.

    Certificate verification is disabled here and *only* here. These readers
    exist to observe, not to certify transport security: the gateway's own
    connection is the one whose `sslmode=verify-full` behaviour is under test,
    and the CA bundle for the managed cluster is not part of the credential
    file. Turning verification off in the observer keeps the observer from
    failing for reasons unrelated to what it is observing.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# --------------------------------------------------------------------------
# Relational upstreams
# --------------------------------------------------------------------------


async def postgres_rows(
    config: LiveConfig, query: str, *args: Any, user: str = "generic_read_user"
) -> list[dict[str, Any]]:
    """Read straight from the live PostgreSQL, bypassing the gateway."""
    conn = await asyncpg.connect(
        host=config.pg_host,
        port=config.pg_port,
        database=config.pg_database,
        user=user,
        password=config.pg_password(user),
        ssl=_managed_tls(),
        # Managed pools front the server with a connection pooler that does
        # not guarantee the same backend per statement, which invalidates
        # asyncpg's prepared-statement cache.
        statement_cache_size=0,
    )
    try:
        return [dict(row) for row in await conn.fetch(query, *args)]
    finally:
        await conn.close()


async def postgres_execute(
    config: LiveConfig, statement: str, *args: Any, user: str = "generic_ddl_user"
) -> str:
    """Run a statement as a named upstream user.

    Used to arrange fixtures and to establish the privilege matrix, never to
    assert a governance outcome.
    """
    conn = await asyncpg.connect(
        host=config.pg_host,
        port=config.pg_port,
        database=config.pg_database,
        user=user,
        password=config.pg_password(user),
        ssl=_managed_tls(),
        statement_cache_size=0,
    )
    try:
        return await conn.execute(statement, *args)
    finally:
        await conn.close()


async def mysql_rows(
    config: LiveConfig, query: str, *args: Any, user: str = "generic_read_user"
) -> list[dict[str, Any]]:
    """Read straight from the live MySQL, bypassing the gateway."""
    import aiomysql

    conn = await aiomysql.connect(
        host=config.mysql_host,
        port=config.mysql_port,
        user=user,
        password=config.mysql_password(user),
        db=config.mysql_database,
        ssl=_managed_tls(),
    )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, args or None)
            return list(await cur.fetchall())
    finally:
        conn.close()


async def mysql_execute(
    config: LiveConfig, statement: str, *args: Any, user: str = "generic_ddl_user"
) -> int:
    import aiomysql

    conn = await aiomysql.connect(
        host=config.mysql_host,
        port=config.mysql_port,
        user=user,
        password=config.mysql_password(user),
        db=config.mysql_database,
        ssl=_managed_tls(),
        autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(statement, args or None)
            return cur.rowcount
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Object storage
# --------------------------------------------------------------------------


def _s3_client(config: LiveConfig) -> Any:
    import boto3

    return boto3.client(
        "s3",
        aws_access_key_id=config.s3_access_key_id,
        aws_secret_access_key=config.s3_secret_access_key,
        region_name=config.s3_region or "us-east-1",
    )


def s3_object_keys(config: LiveConfig, prefix: str | None = None) -> list[str]:
    """List what the bucket actually holds under a prefix."""
    client = _s3_client(config)
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(
        Bucket=config.s3_bucket, Prefix=prefix if prefix is not None else config.s3_run_prefix
    ):
        keys.extend(item["Key"] for item in page.get("Contents", []))
    return sorted(keys)


def object_exists(config: LiveConfig, key: str) -> bool:
    """Whether one object is really present.

    A deletion the gateway reports as done but never performed is the object
    storage form of the Write Safety defect.
    """
    from botocore.exceptions import ClientError

    try:
        _s3_client(config).head_object(Bucket=config.s3_bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def s3_put(config: LiveConfig, key: str, body: bytes) -> None:
    _s3_client(config).put_object(Bucket=config.s3_bucket, Key=key, Body=body)


def s3_delete_prefix(config: LiveConfig, prefix: str) -> int:
    """Remove every object under a prefix. Used only by teardown and the sweeper."""
    client = _s3_client(config)
    keys = s3_object_keys(config, prefix)
    for key in keys:
        client.delete_object(Bucket=config.s3_bucket, Key=key)
    return len(keys)


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------


def _slack_client(config: LiveConfig) -> Any:
    from slack_sdk import WebClient

    return WebClient(token=config.slack_bot_token)


def slack_message_texts(config: LiveConfig, limit: int = 50) -> list[str]:
    """Message text as Slack itself holds it.

    This is what proves redaction: the gateway's response must not contain a
    value that this reader still shows in full.
    """
    response = _slack_client(config).conversations_history(
        channel=config.slack_channel_id, limit=limit
    )
    return [str(message.get("text", "")) for message in response.get("messages", [])]


def slack_post(config: LiveConfig, text: str) -> str:
    """Post a message and return its timestamp id, for later deletion."""
    response = _slack_client(config).chat_postMessage(channel=config.slack_channel_id, text=text)
    return str(response["ts"])


def slack_delete(config: LiveConfig, ts: str) -> None:
    _slack_client(config).chat_delete(channel=config.slack_channel_id, ts=ts)


# --------------------------------------------------------------------------
# Google Workspace
# --------------------------------------------------------------------------


def _drive_service(config: LiveConfig, *, impersonate: bool = False) -> Any:
    """Build a Drive client.

    `impersonate` is opt-in and will fail for a consumer subject account; see
    `LiveConfig.gws_can_impersonate`. The default is the service account
    acting as itself, which can read anything explicitly shared with its own
    address.
    """
    import json

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(config.gws_service_account_json),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    if impersonate and config.gws_subject_user:
        credentials = credentials.with_subject(config.gws_subject_user)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def drive_folder_id(config: LiveConfig) -> str:
    """Resolve the certification folder, by id if configured, else by name.

    Resolving by name is deliberate: a name is what an operator actually
    shares with the service account, and a folder id pasted into the
    environment goes stale as soon as the folder is recreated. Returns an
    empty string when nothing has been shared, which the caller turns into a
    skip with a stated reason rather than an opaque failure.
    """
    if config.gws_drive_folder_id:
        return config.gws_drive_folder_id
    response = (
        _drive_service(config)
        .files()
        .list(
            q=(
                "mimeType = 'application/vnd.google-apps.folder' and trashed = false "
                f"and name = '{config.gws_drive_folder_name}'"
            ),
            pageSize=10,
            fields="files(id,name)",
        )
        .execute()
    )
    matches = response.get("files", [])
    return str(matches[0]["id"]) if matches else ""


def drive_create_text_file(config: LiveConfig, folder_id: str, name: str, body: str) -> str:
    """Create a plain-text file in the shared folder; returns its id."""
    from googleapiclient.http import MediaInMemoryUpload

    created = (
        _drive_service(config)
        .files()
        .create(
            body={"name": name, "parents": [folder_id], "mimeType": "text/plain"},
            media_body=MediaInMemoryUpload(body.encode("utf-8"), mimetype="text/plain"),
            fields="id",
        )
        .execute()
    )
    return str(created["id"])


def drive_delete(config: LiveConfig, file_id: str) -> None:
    _drive_service(config).files().delete(fileId=file_id).execute()


def drive_file_names(config: LiveConfig, *, folder_id: str = "") -> list[str]:
    """Files Drive actually reports as visible to the certifying identity."""
    query = f"'{folder_id}' in parents and trashed = false" if folder_id else "trashed = false"
    response = (
        _drive_service(config)
        .files()
        .list(q=query, pageSize=100, fields="files(id,name,mimeType)")
        .execute()
    )
    return sorted(str(item["name"]) for item in response.get("files", []))


def drive_file_content(config: LiveConfig, file_id: str) -> str:
    return _drive_service(config).files().get_media(fileId=file_id).execute().decode("utf-8")


def drive_files(config: LiveConfig, *, folder_id: str = "") -> list[dict[str, Any]]:
    """Files with their ids and mime types, for callers that need to read content."""
    query = f"'{folder_id}' in parents and trashed = false" if folder_id else "trashed = false"
    response = (
        _drive_service(config)
        .files()
        .list(q=query, pageSize=100, fields="files(id,name,mimeType)")
        .execute()
    )
    return list(response.get("files", []))


def drive_text(config: LiveConfig, file_id: str, mime_type: str) -> str:
    """Read a file's text, exporting Google-native formats.

    A Google Doc has no byte stream to download - `get_media` returns 403 for
    one - so native types are exported to text/plain instead. Returns an empty
    string for anything that is neither, rather than raising: an unreadable
    file should narrow what a test can prove, not break the run.
    """
    service = _drive_service(config)
    try:
        if mime_type.startswith("application/vnd.google-apps"):
            if mime_type == "application/vnd.google-apps.folder":
                return ""
            raw = service.files().export(fileId=file_id, mimeType="text/plain").execute()
        else:
            raw = service.files().get_media(fileId=file_id).execute()
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    except Exception:
        return ""
