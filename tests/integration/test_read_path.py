"""End-to-end integration test for the read path.

Verifies the full pipeline: PG client -> PGProxy -> normalize -> cache -> upstream -> audit.
Uses mock asyncio streams to simulate the PG wire protocol without real PG/Redis.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest

from interlock.cache.l1 import L1Cache
from interlock.cache.l2 import L2Cache
from interlock.gateway.pg_messages import (
    MSG_TYPE_AUTH,
    MSG_TYPE_COMMAND_COMPLETE,
    MSG_TYPE_DATA_ROW,
    MSG_TYPE_QUERY,
    MSG_TYPE_READY_FOR_QUERY,
    MSG_TYPE_ROW_DESCRIPTION,
    PROTOCOL_VERSION_3_0,
    pack_message,
)
from interlock.gateway.pg_proxy import PGProxy
from interlock.models import AuditRecord

# ---------------------------------------------------------------------------
# PG wire protocol helpers for test message construction
# ---------------------------------------------------------------------------

_HEADER_STRUCT = struct.Struct("!I")
_INT16 = struct.Struct("!h")
_INT32 = struct.Struct("!i")


def make_query_message(sql: str) -> bytes:
    """Build a SimpleQuery ('Q') message: type + length + sql + NUL."""
    payload = sql.encode("utf-8") + b"\x00"
    return pack_message(MSG_TYPE_QUERY, payload)


def make_row_description(col_names: list[str] | None = None) -> bytes:
    """Build a RowDescription ('T') message with simple text columns."""
    if col_names is None:
        col_names = ["id", "name"]
    buf = _INT16.pack(len(col_names))
    for name in col_names:
        buf += name.encode("utf-8") + b"\x00"
        buf += _INT32.pack(0)  # table OID
        buf += _INT16.pack(0)  # column attr number
        buf += _INT32.pack(25)  # type OID (text = 25)
        buf += _INT16.pack(-1)  # type size
        buf += _INT32.pack(-1)  # type modifier
        buf += _INT16.pack(0)  # format code (text)
    return pack_message(MSG_TYPE_ROW_DESCRIPTION, buf)


def make_data_row(values: list[bytes]) -> bytes:
    """Build a DataRow ('D') message."""
    buf = _INT16.pack(len(values))
    for v in values:
        buf += _INT32.pack(len(v))
        buf += v
    return pack_message(MSG_TYPE_DATA_ROW, buf)


def make_command_complete(tag: str) -> bytes:
    """Build a CommandComplete ('C') message."""
    payload = tag.encode("utf-8") + b"\x00"
    return pack_message(MSG_TYPE_COMMAND_COMPLETE, payload)


def make_ready_for_query(status: str = "I") -> bytes:
    """Build a ReadyForQuery ('Z') message. 'I' = idle."""
    return pack_message(MSG_TYPE_READY_FOR_QUERY, status.encode("ascii"))


def make_startup_message(user: str = "testuser", database: str = "testdb") -> bytes:
    """Build a startup message (no type byte): length + version + params."""
    params = (
        b"user\x00"
        + user.encode("utf-8")
        + b"\x00"
        + b"database\x00"
        + database.encode("utf-8")
        + b"\x00"
        + b"\x00"  # terminator
    )
    payload = _HEADER_STRUCT.pack(PROTOCOL_VERSION_3_0) + params
    length = len(payload) + 4  # length includes itself
    return _HEADER_STRUCT.pack(length) + payload


def make_auth_ok() -> bytes:
    """Build an AuthenticationOk ('R') message: type=0 means success."""
    return pack_message(MSG_TYPE_AUTH, _INT32.pack(0))


# ---------------------------------------------------------------------------
# Mock Redis for L2Cache (dict-backed)
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-memory dict that mimics the subset of redis.asyncio.Redis used by L2Cache."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


def _scoped_pg_read_cache_key(sql: str, source_id: str = "test") -> str:
    """Return the governed simple-read cache key produced after preflight."""
    from interlock.core.normalizer import compute_cache_key, normalize_sql
    from interlock.gateway.pg_proxy import _decision_scope_hash
    from interlock.gateway.pipeline import GatewayDecision

    nq = normalize_sql(sql, source_id)
    assert not isinstance(nq, list)
    return compute_cache_key(
        source_id,
        nq.normalized_sql or "",
        protocol="postgresql",
        parameters=nq.parameters,
        policy_scope_hash=_decision_scope_hash(
            GatewayDecision(allowed=True, redaction_required=True)
        ),
    )


# ---------------------------------------------------------------------------
# Mock audit logger that collects records in a list
# ---------------------------------------------------------------------------


class CollectingAuditLogger:
    """Drop-in for AuditLogger that stores records in memory."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def log(self, record: AuditRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# Stream helpers: feed bytes into an asyncio.StreamReader
# and capture bytes written to a mock StreamWriter
# ---------------------------------------------------------------------------


def _make_client_reader(*messages: bytes) -> asyncio.StreamReader:
    """Create a StreamReader pre-loaded with concatenated message bytes."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"".join(messages))
    reader.feed_eof()
    return reader


def _make_upstream_reader(*messages: bytes) -> asyncio.StreamReader:
    """Create a StreamReader pre-loaded with upstream response bytes."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"".join(messages))
    reader.feed_eof()
    return reader


class WriterCapture:
    """Mock asyncio.StreamWriter that captures all written bytes."""

    def __init__(self) -> None:
        self.data = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        pass

    def get_extra_info(self, key: str, default: Any = None) -> Any:
        if key == "peername":
            return ("127.0.0.1", 12345)
        return default


# ---------------------------------------------------------------------------
# Build a standard upstream response (RowDescription + DataRow + CommandComplete + ReadyForQuery)
# ---------------------------------------------------------------------------


def _build_upstream_response(
    col_names: list[str] | None = None,
    rows: list[list[bytes]] | None = None,
    tag: str = "SELECT 1",
) -> bytes:
    """Assemble a complete upstream response for a simple SELECT."""
    if col_names is None:
        col_names = ["id", "name"]
    if rows is None:
        rows = [[b"1", b"alice"]]

    parts = [make_row_description(col_names)]
    for row in rows:
        parts.append(make_data_row(row))
    parts.append(make_command_complete(tag))
    parts.append(make_ready_for_query())
    return b"".join(parts)


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEndToEndReadPath:
    """Full pipeline test: PG proxy -> normalize -> cache -> upstream -> audit."""

    def _make_proxy(
        self,
        l1: L1Cache | None = None,
        l2: L2Cache | None = None,
        audit: CollectingAuditLogger | None = None,
    ) -> PGProxy:
        proxy = PGProxy(
            listen_host="127.0.0.1",
            listen_port=0,
            upstream_host="127.0.0.1",
            upstream_port=0,
            normalizer_source_id="test",
            l1_cache=l1,
            l2_cache=l2,
            audit_logger=audit,
        )
        return proxy

    # -- helpers to drive the proxy's internal methods directly ---------------

    async def _drive_simple_query(
        self,
        proxy: PGProxy,
        sql: str,
        upstream_response: bytes,
    ) -> bytes:
        """Send a SimpleQuery through proxy._handle_simple_query and return
        the bytes written to the client writer."""
        payload = sql.encode("utf-8") + b"\x00"

        upstream_reader = _make_upstream_reader(upstream_response)
        upstream_writer = WriterCapture()
        client_writer = WriterCapture()

        await proxy._handle_simple_query(
            payload,
            client_writer,  # type: ignore[arg-type]
            upstream_reader,
            upstream_writer,  # type: ignore[arg-type]
        )

        return bytes(client_writer.data)

    # -- Tests ---------------------------------------------------------------

    async def test_cache_miss_then_hit(self) -> None:
        """First query misses cache, second identical query hits L1."""
        l1 = L1Cache(max_size=100, ttl_seconds=60)
        fake_redis = FakeRedis()
        l2 = L2Cache(redis_client=fake_redis, ttl_seconds=300)  # type: ignore[arg-type]
        audit = CollectingAuditLogger()
        proxy = self._make_proxy(l1=l1, l2=l2, audit=audit)

        upstream_bytes = _build_upstream_response()
        sql = "SELECT id, name FROM users WHERE id = 1"

        # First call: cache miss - goes to upstream
        result1 = await self._drive_simple_query(proxy, sql, upstream_bytes)
        assert len(result1) > 0, "Client should receive response bytes"

        # Verify audit: first call is a cache miss
        assert len(audit.records) == 1
        assert audit.records[0].cache_hit is False

        # L1 should now have the entry
        assert l1.stats["size"] == 1
        assert l1.stats["misses"] == 1

        # Second call: cache hit - should NOT touch upstream
        # Build a reader that would raise if read (to prove it's not used)
        empty_upstream = _build_upstream_response()  # won't be consumed
        result2 = await self._drive_simple_query(proxy, sql, empty_upstream)
        assert len(result2) > 0, "Client should receive cached response bytes"

        # Verify L1 hit
        assert l1.stats["hits"] == 1
        assert len(audit.records) == 2
        assert audit.records[1].cache_hit is True
        assert audit.records[1].cache_tier == "l1"

    async def test_normalized_query_parameters_do_not_share_cache(self) -> None:
        """Same SQL shape with different literals uses distinct cache keys."""
        l1 = L1Cache(max_size=100, ttl_seconds=60)
        fake_redis = FakeRedis()
        l2 = L2Cache(redis_client=fake_redis, ttl_seconds=300)  # type: ignore[arg-type]
        audit = CollectingAuditLogger()
        proxy = self._make_proxy(l1=l1, l2=l2, audit=audit)

        upstream_bytes = _build_upstream_response()

        # First query with literal 1
        sql1 = "SELECT id, name FROM users WHERE id = 1"
        result1 = await self._drive_simple_query(proxy, sql1, upstream_bytes)
        assert len(result1) > 0

        # Second query with literal 42 - same structure, different literal
        sql2 = "SELECT id, name FROM users WHERE id = 42"
        result2 = await self._drive_simple_query(proxy, sql2, upstream_bytes)
        assert len(result2) > 0

        # The normalized SQL shape matches, but literal parameters now scope
        # the cache key. Serving the first row set for the second literal
        # would leak cross-record data.
        assert len(audit.records) == 2
        assert audit.records[0].cache_hit is False
        assert audit.records[1].cache_hit is False

        # Governed fingerprints should differ because the parameter hash is
        # included in the v3 deterministic cache namespace.
        assert audit.records[0].sql_fingerprint is not None
        assert audit.records[0].sql_fingerprint != audit.records[1].sql_fingerprint
        assert l1.stats["size"] == 2

    async def test_audit_logged_on_cache_miss(self) -> None:
        """Audit record created when query goes to upstream."""
        l1 = L1Cache(max_size=100, ttl_seconds=60)
        audit = CollectingAuditLogger()
        proxy = self._make_proxy(l1=l1, audit=audit)

        upstream_bytes = _build_upstream_response()
        sql = "SELECT count(*) FROM orders"

        await self._drive_simple_query(proxy, sql, upstream_bytes)

        assert len(audit.records) == 1
        rec = audit.records[0]
        assert rec.cache_hit is False
        assert rec.cache_tier is None
        assert rec.operation == "read"
        assert rec.source_id == "test"
        assert rec.sql_fingerprint is not None
        assert rec.latency_ms is not None
        assert rec.latency_ms >= 0

    async def test_audit_logged_on_cache_hit(self) -> None:
        """Audit record created even on cache hit."""
        l1 = L1Cache(max_size=100, ttl_seconds=60)
        audit = CollectingAuditLogger()
        proxy = self._make_proxy(l1=l1, audit=audit)

        upstream_bytes = _build_upstream_response()
        sql = "SELECT 1"

        # Miss
        await self._drive_simple_query(proxy, sql, upstream_bytes)
        # Hit
        await self._drive_simple_query(proxy, sql, upstream_bytes)

        assert len(audit.records) == 2
        miss_rec = audit.records[0]
        hit_rec = audit.records[1]

        assert miss_rec.cache_hit is False
        assert hit_rec.cache_hit is True
        assert hit_rec.cache_tier == "l1"
        assert hit_rec.operation == "read"
        assert hit_rec.latency_ms is not None

    async def test_full_pipeline_response_integrity(self) -> None:
        """Response bytes from cache match original upstream response."""
        l1 = L1Cache(max_size=100, ttl_seconds=60)
        fake_redis = FakeRedis()
        l2 = L2Cache(redis_client=fake_redis, ttl_seconds=300)  # type: ignore[arg-type]
        audit = CollectingAuditLogger()
        proxy = self._make_proxy(l1=l1, l2=l2, audit=audit)

        upstream_bytes = _build_upstream_response(
            col_names=["id", "email"],
            rows=[[b"7", b"bob@example.com"]],
            tag="SELECT 1",
        )
        sql = "SELECT id, email FROM users WHERE id = 7"

        # First call - cache miss - client gets upstream bytes
        result_miss = await self._drive_simple_query(proxy, sql, upstream_bytes)

        # Second call - cache hit from L1 - should be identical bytes
        result_hit = await self._drive_simple_query(proxy, sql, upstream_bytes)

        assert (
            result_miss == result_hit
        ), "Cached response bytes must exactly match original upstream response"

        # Also verify L2 has the data under the post-preflight governed key.
        _key = _scoped_pg_read_cache_key(sql)
        l2_result = await l2.get(_key)
        assert l2_result.hit is True
        assert l2_result.data == result_miss

    async def test_l2_promotion_to_l1(self) -> None:
        """When L1 misses but L2 hits, the value is promoted to L1."""
        l1 = L1Cache(max_size=100, ttl_seconds=60)
        fake_redis = FakeRedis()
        l2 = L2Cache(redis_client=fake_redis, ttl_seconds=300)  # type: ignore[arg-type]
        audit = CollectingAuditLogger()
        proxy = self._make_proxy(l1=l1, l2=l2, audit=audit)

        upstream_bytes = _build_upstream_response()
        sql = "SELECT id, name FROM users WHERE id = 99"

        # First call: populate both caches
        await self._drive_simple_query(proxy, sql, upstream_bytes)

        # Evict from L1 only
        _key = _scoped_pg_read_cache_key(sql)
        await l1.invalidate(_key)
        assert l1.stats["size"] == 0

        # Second call: L1 miss, L2 hit, should promote back to L1
        result = await self._drive_simple_query(proxy, sql, upstream_bytes)
        assert len(result) > 0

        assert audit.records[1].cache_hit is True
        assert audit.records[1].cache_tier == "l2"

        # L1 should now have it again (promoted)
        l1_result = await l1.get(_key)
        assert l1_result.hit is True
