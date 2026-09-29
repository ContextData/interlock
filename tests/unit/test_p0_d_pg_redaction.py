"""Regression test for audit P0-D: PG response PII is redacted, not just detected.

AUDIT-COVERS: P0-D

The audit reported that ``pg_proxy.py`` only *detected* PII after the
response had already been forwarded to the client. The fix: collect
upstream silently, walk DataRow messages, rewrite text fields with
``[REDACTED:<TYPE>]``, and only then forward the rewritten bytes.

These tests pin the structural rewrite so the wire format stays valid
and PII never reaches the client.
"""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import MagicMock

import pytest

from interlock.gateway.pg_messages import (
    MSG_TYPE_READY_FOR_QUERY,
    pack_message,
)
from interlock.gateway.pg_proxy import PGProxy
from interlock.pipeline.pii_fast import PIIFastScanner


def _data_row(*fields: str | None) -> bytes:
    """Build a PG DataRow ('D') message from string fields (None -> NULL)."""
    body = struct.pack(">H", len(fields))
    for f in fields:
        if f is None:
            body += struct.pack(">i", -1)
        else:
            b = f.encode("utf-8")
            body += struct.pack(">i", len(b)) + b
    # Length prefix is self-inclusive: 4 + len(body)
    return b"D" + struct.pack(">I", 4 + len(body)) + body


def _ready_for_query() -> bytes:
    return pack_message(MSG_TYPE_READY_FOR_QUERY, b"I")


def _parse_data_rows(stream: bytes) -> list[list[str | None]]:
    """Parse 'D' messages from a stream and return field lists."""
    rows: list[list[str | None]] = []
    offset = 0
    n = len(stream)
    while offset < n:
        msg_type = stream[offset : offset + 1]
        msg_len = struct.unpack(">I", stream[offset + 1 : offset + 5])[0]
        end = offset + 1 + msg_len
        if msg_type == b"D":
            field_count = struct.unpack(">H", stream[offset + 5 : offset + 7])[0]
            pos = offset + 7
            fields: list[str | None] = []
            for _ in range(field_count):
                flen = struct.unpack(">i", stream[pos : pos + 4])[0]
                pos += 4
                if flen < 0:
                    fields.append(None)
                else:
                    fields.append(stream[pos : pos + flen].decode("utf-8"))
                    pos += flen
            rows.append(fields)
        offset = end
    return rows


def _make_proxy() -> PGProxy:
    return PGProxy(listen_port=0, upstream_port=0, pii_scanner=PIIFastScanner())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_p0_d_redact_replaces_ssn_in_data_row() -> None:
    proxy = _make_proxy()
    stream = _data_row("alice", "ssn:123-45-6789") + _ready_for_query()
    out, detected, types = proxy._redact_response_bytes(stream)
    assert detected is True
    assert "SSN" in types
    rows = _parse_data_rows(out)
    assert rows == [["alice", "ssn:[REDACTED:SSN]"]]


def test_p0_d_redact_replaces_multiple_pii_types() -> None:
    proxy = _make_proxy()
    stream = (
        _data_row("contact: alice@example.com", "phone: 555-123-4567", "ip: 10.0.0.1")
        + _ready_for_query()
    )
    out, detected, types = proxy._redact_response_bytes(stream)
    assert detected is True
    assert {"EMAIL", "PHONE", "IP_ADDRESS"} <= set(types)
    rows = _parse_data_rows(out)
    for row in rows:
        for cell in row:
            assert "alice@example.com" not in (cell or "")
            assert "555-123-4567" not in (cell or "")
            assert "10.0.0.1" not in (cell or "")


def test_p0_d_redact_preserves_null_fields() -> None:
    proxy = _make_proxy()
    stream = _data_row("ok", None, "ssn 999-88-7777") + _ready_for_query()
    out, _, _ = proxy._redact_response_bytes(stream)
    rows = _parse_data_rows(out)
    assert rows[0][1] is None
    assert "999-88-7777" not in (rows[0][2] or "")


def test_p0_d_redact_passes_non_data_row_through() -> None:
    proxy = _make_proxy()
    # CommandComplete + ReadyForQuery (no DataRows).
    cc = b"C" + struct.pack(">I", 4 + len(b"SELECT 0\x00")) + b"SELECT 0\x00"
    stream = cc + _ready_for_query()
    out, detected, _ = proxy._redact_response_bytes(stream)
    assert out == stream
    assert detected is False


def test_p0_d_redact_no_scanner_returns_original_bytes() -> None:
    proxy = PGProxy(listen_port=0, upstream_port=0)  # no pii_scanner
    stream = _data_row("ssn 123-45-6789") + _ready_for_query()
    out, detected, types = proxy._redact_response_bytes(stream)
    assert out == stream
    assert detected is False
    assert types == []


def test_p0_d_redact_handles_clean_rows_without_modification() -> None:
    proxy = _make_proxy()
    stream = _data_row("hello", "world") + _ready_for_query()
    out, detected, _ = proxy._redact_response_bytes(stream)
    assert out == stream
    assert detected is False


@pytest.mark.asyncio
async def test_p0_d_simple_query_forwards_redacted_bytes_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end-ish: cache miss path collects upstream, redacts, forwards.

    We feed mock upstream reader bytes containing PII and verify the
    bytes written to the client contain redaction tokens, never the
    original PII.
    """
    proxy = _make_proxy()

    upstream_response = _data_row("ssn 123-45-6789") + _ready_for_query()

    upstream_reader = MagicMock()

    # Drive _collect_silent via read_message stub.
    msg_iter = [
        ("D", upstream_response[5 : 5 + struct.unpack(">I", upstream_response[1:5])[0] - 4]),
        (MSG_TYPE_READY_FOR_QUERY, b"I"),
    ]
    idx = {"i": 0}

    async def fake_read_message(reader, **_kwargs):
        i = idx["i"]
        idx["i"] += 1
        if i >= len(msg_iter):
            raise asyncio.IncompleteReadError(b"", 1)
        return msg_iter[i]

    monkeypatch.setattr("interlock.gateway.pg_proxy.read_message", fake_read_message)

    upstream_writer = MagicMock()
    upstream_writer.write = MagicMock()
    upstream_writer.drain = MagicMock(side_effect=lambda: asyncio.sleep(0))

    captured = bytearray()

    class _ClientWriter:
        def write(self, data):
            captured.extend(data)

        async def drain(self):
            pass

    client_writer = _ClientWriter()

    sql_payload = b"SELECT 1\x00"
    await proxy._handle_simple_query(sql_payload, client_writer, upstream_reader, upstream_writer)

    # The client must NEVER have seen the raw SSN.
    assert b"123-45-6789" not in bytes(captured)
    assert b"[REDACTED:SSN]" in bytes(captured)
