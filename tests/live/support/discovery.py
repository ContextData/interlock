"""Index real assets from the live upstreams into the discovery catalogue.

S3, Slack and Google Workspace all declare `supports_query=False`; their
governed path is discovery, which searches an index rather than the upstream
directly. So to certify governance over them, the index has to contain
something - and if that something were a hand-written fixture, the
certification would prove governance over a fixture rather than over the
customer's data.

This module therefore indexes what the upstreams *actually contain*, read
through `effects.py`: real object keys, real channel messages, real Drive
files. A discovery result is then a statement about the live system, and
redaction observed on it is redaction of real content.

Everything indexed is removed by `teardown_discovery`, keyed on the same
`live_cert_` source prefix the rest of the harness uses.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from tests.live.support import effects
from tests.live.support.config import SOURCE_ID_PREFIX, LiveConfig
from tests.live.support.seed import source_id

_UPSERT = """
INSERT INTO discovery_assets
    (source_id, asset_type, asset_path, title, summary, topics,
     search_vector, quality_score, metadata)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, to_tsvector('english', $7), 0.9, $8::jsonb)
ON CONFLICT (source_id, asset_type, asset_path) DO UPDATE
SET title = EXCLUDED.title,
    summary = EXCLUDED.summary,
    topics = EXCLUDED.topics,
    search_vector = EXCLUDED.search_vector,
    metadata = EXCLUDED.metadata,
    updated_at = NOW()
"""


async def _index(
    conn: asyncpg.Connection,
    *,
    source: str,
    asset_type: str,
    asset_path: str,
    title: str,
    body: str,
    topics: list[str],
) -> None:
    await conn.execute(
        _UPSERT,
        source,
        asset_type,
        asset_path,
        title,
        json.dumps({"title": title, "body": body}),
        topics,
        f"{title} {body}",
        json.dumps({"seed": "live-cert"}),
    )


async def index_s3(conn: asyncpg.Connection, cfg: LiveConfig, limit: int = 10) -> int:
    """Index real object keys from the live bucket."""
    keys = effects.s3_object_keys(cfg, prefix=cfg.s3_prefix)[:limit]
    for key in keys:
        await _index(
            conn,
            source=source_id("s3"),
            asset_type="file",
            asset_path=f"s3://{cfg.s3_bucket}/{key}",
            title=key.rsplit("/", 1)[-1] or key,
            body=f"object {key} in bucket {cfg.s3_bucket}",
            topics=["s3", "object", "live-cert"],
        )
    return len(keys)


async def index_slack(conn: asyncpg.Connection, cfg: LiveConfig, limit: int = 20) -> int:
    """Index real channel messages, text and all.

    The message text is indexed verbatim, which is what makes redaction
    provable: a message containing a synthetic SSN reaches the catalogue with
    the SSN in it, and the governed read must not return it.
    """
    texts = effects.slack_message_texts(cfg, limit=limit)
    for position, text in enumerate(texts):
        if not text.strip():
            continue
        await _index(
            conn,
            source=source_id("slack"),
            asset_type="message",
            asset_path=f"slack://channel/{cfg.slack_channel_id}/{position}",
            title=f"message {position} in #{cfg.slack_channel_name}",
            body=text,
            topics=["slack", "message", "live-cert"],
        )
    return len(texts)


async def index_drive(conn: asyncpg.Connection, cfg: LiveConfig) -> int:
    """Index real files from the shared Drive folder, including their text.

    The *content* is indexed, not just the filename. Indexing only names would
    make the Drive redaction test unfalsifiable - there would be no PII in the
    catalogue to redact, and the test would pass by having nothing to find.
    """
    folder = effects.drive_folder_id(cfg)
    if not folder:
        return 0
    files = effects.drive_files(cfg, folder_id=folder)
    for item in files:
        text = effects.drive_text(cfg, item["id"], item.get("mimeType", ""))
        await _index(
            conn,
            source=source_id("google_workspace"),
            asset_type="document",
            asset_path=f"gdrive://{folder}/{item['id']}",
            title=str(item["name"]),
            body=text or f"Drive document {item['name']} in the certification folder",
            topics=["drive", "document", "live-cert"],
        )
    return len(files)


async def index_all(cfg: LiveConfig) -> dict[str, int]:
    conn = await asyncpg.connect(cfg.e2e.control_dsn)
    counts: dict[str, int] = {}
    try:
        if cfg.has_s3():
            counts["s3"] = await index_s3(conn, cfg)
        if cfg.has_slack():
            counts["slack"] = await index_slack(conn, cfg)
        if cfg.has_google_workspace():
            counts["google_workspace"] = await index_drive(conn, cfg)
        return counts
    finally:
        await conn.close()


async def teardown_discovery(cfg: LiveConfig) -> int:
    conn = await asyncpg.connect(cfg.e2e.control_dsn)
    try:
        status = await conn.execute(
            "DELETE FROM discovery_assets WHERE source_id LIKE $1", f"{SOURCE_ID_PREFIX}%"
        )
        tail = status.rsplit(" ", 1)[-1]
        return int(tail) if tail.isdigit() else 0
    finally:
        await conn.close()


async def indexed_paths(cfg: LiveConfig, source: str) -> list[dict[str, Any]]:
    """What the catalogue holds for one source, for ground-truth comparison."""
    conn = await asyncpg.connect(cfg.e2e.control_dsn)
    try:
        rows = await conn.fetch(
            "SELECT asset_path, title, summary FROM discovery_assets WHERE source_id = $1",
            source,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()
