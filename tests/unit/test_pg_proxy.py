"""Unit tests for PG wire protocol helpers and PGProxy."""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interlock.cache.provenance import with_redaction
from interlock.core.write_classifier import WriteClassifier
from interlock.errors import AuditUnavailableError, IdentityNotFoundError
from interlock.gateway.pg_messages import (
    CANCEL_REQUEST_CODE,
    MSG_TYPE_AUTH,
    MSG_TYPE_COMMAND_COMPLETE,
    MSG_TYPE_QUERY,
    MSG_TYPE_READY_FOR_QUERY,
    MSG_TYPE_TERMINATE,
    PROTOCOL_VERSION_3_0,
    SSL_REQUEST_CODE,
    extract_sql_from_parse,
    extract_sql_from_query,
    is_ssl_request,
    pack_message,
    read_message,
    read_startup_message,
    write_message,
)
from interlock.gateway.pg_proxy import (
    ExtendedExecutionContext,
    PGCancelTarget,
    PGProxy,
    UpstreamConnectionRefusedError,
    _open_upstream_connection,
)

# ---------------------------------------------------------------------------
# Helpers for building mock asyncio streams
# ---------------------------------------------------------------------------


def _make_reader(data: bytes) -> asyncio.StreamReader:
    """Return a StreamReader pre-loaded with *data*."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _make_writer() -> tuple[asyncio.StreamWriter, bytearray]:
    """Return a (writer, buffer) pair where writes accumulate in *buffer*."""
    buf = bytearray()
    transport = MagicMock()
    transport.is_closing.return_value = False

    protocol = MagicMock()

    writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_event_loop())

    # Patch write to capture bytes
    original_write = writer.write

    def _capture_write(data: bytes | bytearray | memoryview) -> None:
        buf.extend(data)

    writer.write = _capture_write  # type: ignore[assignment]
    writer.drain = AsyncMock()  # type: ignore[method-assign]
    writer.close = MagicMock()  # type: ignore[method-assign]
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))  # type: ignore[method-assign]

    return writer, buf


def _pack_startup(version: int = PROTOCOL_VERSION_3_0) -> bytes:
    """Build a minimal startup message (length + version + double NUL)."""
    payload = struct.pack("!I", version) + b"user\x00test\x00\x00"
    length = 4 + len(payload)
    return struct.pack("!I", length) + payload


def _pack_ssl_request() -> bytes:
    """Build an SSL request message."""
    return struct.pack("!II", 8, SSL_REQUEST_CODE)


def _pack_cancel_request(process_id: int, secret_key: int) -> bytes:
    """Build the special startup-phase PostgreSQL CancelRequest packet."""
    return struct.pack("!IIII", 16, CANCEL_REQUEST_CODE, process_id, secret_key)


def _pack_query(sql: str) -> bytes:
    """Build a SimpleQuery message (type + length + sql + NUL)."""
    payload = sql.encode("utf-8") + b"\x00"
    return pack_message(MSG_TYPE_QUERY, payload)


def _pack_parse(statement_name: str, sql: str) -> bytes:
    payload = (
        statement_name.encode("utf-8")
        + b"\x00"
        + sql.encode("utf-8")
        + b"\x00"
        + struct.pack("!H", 0)
    )
    return pack_message("P", payload)


def _pack_bind(portal_name: str, statement_name: str) -> bytes:
    payload = (
        portal_name.encode("utf-8")
        + b"\x00"
        + statement_name.encode("utf-8")
        + b"\x00"
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
    )
    return pack_message("B", payload)


def _pack_execute(portal_name: str) -> bytes:
    return pack_message("E", portal_name.encode("utf-8") + b"\x00" + struct.pack("!I", 0))


def _pack_describe_statement(statement_name: str) -> bytes:
    return pack_message("D", b"S" + statement_name.encode("utf-8") + b"\x00")


def _pack_ready_for_query(status: str = "I") -> bytes:
    """Build a ReadyForQuery message."""
    return pack_message(MSG_TYPE_READY_FOR_QUERY, status.encode("ascii"))


def _pack_auth_ok() -> bytes:
    """Build an AuthenticationOk message (R, subtype 0)."""
    return pack_message(MSG_TYPE_AUTH, struct.pack("!I", 0))


def _pack_command_complete(tag: str = "SELECT 1") -> bytes:
    """Build a CommandComplete message."""
    return pack_message(MSG_TYPE_COMMAND_COMPLETE, tag.encode("utf-8") + b"\x00")


def _pack_close_statement(statement_name: str) -> bytes:
    return pack_message("C", b"S" + statement_name.encode("utf-8") + b"\x00")


# =========================================================================
# pg_messages helpers
# =========================================================================


class TestReadWriteMessage:
    """Test read_message / write_message / pack_message roundtrip."""

    @pytest.mark.asyncio
    async def test_roundtrip(self) -> None:
        """pack_message then read_message should return the original type + payload."""
        payload = b"SELECT 1\x00"
        raw = pack_message("Q", payload)

        reader = _make_reader(raw)
        msg_type, read_payload = await read_message(reader)

        assert msg_type == "Q"
        assert read_payload == payload

    @pytest.mark.asyncio
    async def test_empty_payload(self) -> None:
        """Messages with zero-length payload should round-trip."""
        raw = pack_message("S", b"")
        reader = _make_reader(raw)
        msg_type, read_payload = await read_message(reader)
        assert msg_type == "S"
        assert read_payload == b""

    @pytest.mark.asyncio
    async def test_write_message_buffers(self) -> None:
        """write_message should buffer bytes into the writer."""
        writer, buf = _make_writer()
        write_message(writer, "Q", b"hello\x00")
        assert len(buf) > 0
        # Verify the type byte
        assert chr(buf[0]) == "Q"

    @pytest.mark.asyncio
    async def test_rejects_oversized_payload_before_reading_body(self) -> None:
        raw = b"Q" + struct.pack("!I", 4 + 9) + b"x" * 9
        reader = _make_reader(raw)

        with pytest.raises(ValueError, match="exceeds"):
            await read_message(reader, max_payload_bytes=8)


class TestReadStartupMessage:
    """Test read_startup_message."""

    @pytest.mark.asyncio
    async def test_protocol_v3(self) -> None:
        raw = _pack_startup(PROTOCOL_VERSION_3_0)
        reader = _make_reader(raw)
        code, data = await read_startup_message(reader)
        assert code == PROTOCOL_VERSION_3_0
        assert data == raw

    @pytest.mark.asyncio
    async def test_ssl_request(self) -> None:
        raw = _pack_ssl_request()
        reader = _make_reader(raw)
        code, data = await read_startup_message(reader)
        assert code == SSL_REQUEST_CODE
        assert data == raw

    @pytest.mark.asyncio
    async def test_rejects_oversized_startup_payload(self) -> None:
        payload = struct.pack("!I", PROTOCOL_VERSION_3_0) + b"x" * 9
        raw = struct.pack("!I", 4 + len(payload)) + payload
        reader = _make_reader(raw)

        with pytest.raises(ValueError, match="exceeds"):
            await read_startup_message(reader, max_payload_bytes=8)


class TestExtractSQL:
    """Test SQL extraction from Q and P payloads."""

    def test_query_simple(self) -> None:
        sql = extract_sql_from_query(b"SELECT 1\x00")
        assert sql == "SELECT 1"

    def test_query_no_nul(self) -> None:
        sql = extract_sql_from_query(b"SELECT 1")
        assert sql == "SELECT 1"

    def test_query_empty(self) -> None:
        sql = extract_sql_from_query(b"\x00")
        assert sql == ""

    def test_parse_named_statement(self) -> None:
        # stmt_name='' (unnamed), sql='SELECT $1', 0 params
        payload = b"\x00SELECT $1\x00" + struct.pack("!H", 0)
        sql = extract_sql_from_parse(payload)
        assert sql == "SELECT $1"

    def test_parse_named_statement_with_name(self) -> None:
        payload = b"mystmt\x00SELECT 42\x00" + struct.pack("!H", 0)
        sql = extract_sql_from_parse(payload)
        assert sql == "SELECT 42"

    def test_parse_empty_sql(self) -> None:
        payload = b"\x00\x00" + struct.pack("!H", 0)
        sql = extract_sql_from_parse(payload)
        assert sql == ""


class TestIsSSLRequest:
    """Test is_ssl_request."""

    def test_valid_ssl_request(self) -> None:
        data = _pack_ssl_request()
        assert is_ssl_request(data) is True

    def test_startup_not_ssl(self) -> None:
        data = _pack_startup()
        assert is_ssl_request(data) is False

    def test_too_short(self) -> None:
        assert is_ssl_request(b"\x00\x00\x00") is False

    def test_wrong_code(self) -> None:
        data = struct.pack("!II", 8, 12345)
        assert is_ssl_request(data) is False


# =========================================================================
# PGProxy
# =========================================================================


class TestPGProxySSLNegotiation:
    """Test that PGProxy handles SSL requests correctly."""

    @pytest.mark.asyncio
    async def test_ssl_then_startup(self) -> None:
        """Client sends SSL request, proxy responds 'N', then reads real startup."""
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
        )

        ssl_data = _pack_ssl_request()
        startup_data = _pack_startup()
        reader = _make_reader(ssl_data + startup_data)
        writer, buf = _make_writer()

        result = await proxy._handle_startup(reader, writer)

        # Proxy should have sent 'N' for SSL
        assert buf == bytearray(b"N")
        # Result should be the startup message
        assert result == startup_data

    @pytest.mark.asyncio
    async def test_no_ssl_direct_startup(self) -> None:
        """Client sends startup directly without SSL request."""
        proxy = PGProxy(listen_port=0, upstream_port=0)

        startup_data = _pack_startup()
        reader = _make_reader(startup_data)
        writer, buf = _make_writer()

        result = await proxy._handle_startup(reader, writer)

        # No 'N' sent
        assert buf == bytearray()
        assert result == startup_data

    @pytest.mark.asyncio
    async def test_required_client_tls_rejects_direct_cleartext_startup(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0, require_client_tls=True)
        reader = _make_reader(_pack_startup())
        writer, buf = _make_writer()

        result = await proxy._handle_startup(reader, writer)

        assert result is None
        assert b"PostgreSQL client TLS is required" in bytes(buf)

    @pytest.mark.asyncio
    async def test_ssl_request_upgrades_before_reading_credentials(self) -> None:
        context = MagicMock()
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            client_ssl_context=context,
            require_client_tls=True,
        )
        reader = _make_reader(_pack_ssl_request() + _pack_startup())
        writer, buf = _make_writer()
        writer.start_tls = AsyncMock()  # type: ignore[method-assign]

        result = await proxy._handle_startup(reader, writer)

        assert result == _pack_startup()
        assert bytes(buf) == b"S"
        writer.start_tls.assert_awaited_once_with(context)


class TestPGProxyP0RuntimeGuards:
    """P0 protocol/resource guardrails fail closed before governance bypass."""

    @pytest.mark.asyncio
    async def test_startup_timeout_sends_fatal_and_returns_none(self, monkeypatch) -> None:
        async def _slow_startup(_reader, **_kwargs):
            await asyncio.sleep(1)

        monkeypatch.setattr("interlock.gateway.pg_proxy.read_startup_message", _slow_startup)
        proxy = PGProxy(listen_port=0, upstream_port=0, startup_timeout_seconds=0.001)
        reader = _make_reader(b"")
        writer, buf = _make_writer()

        result = await proxy._handle_startup(reader, writer)

        assert result is None
        assert b"SFATAL\x00" in bytes(buf)
        assert b"startup timed out" in bytes(buf)

    @pytest.mark.asyncio
    async def test_auth_timeout_rejects_identity(self, monkeypatch) -> None:
        async def _slow_message(_reader, **_kwargs):
            await asyncio.sleep(1)

        monkeypatch.setattr("interlock.gateway.pg_proxy.read_message", _slow_message)
        proxy = PGProxy(listen_port=0, upstream_port=0, auth_timeout_seconds=0.001)
        reader = _make_reader(b"")
        writer, _ = _make_writer()

        with pytest.raises(IdentityNotFoundError, match="timed out"):
            await proxy._authenticate_pg_client(reader, writer, "agent")

    @pytest.mark.asyncio
    async def test_max_connection_admission_rejects_before_startup(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0, max_connections=1)
        proxy._active_connections = 1
        reader = _make_reader(_pack_startup())
        writer, buf = _make_writer()

        await proxy._handle_client(reader, writer)

        assert proxy.active_connections == 1
        assert b"SFATAL\x00" in bytes(buf)
        assert b"too many PostgreSQL proxy connections" in bytes(buf)

    @pytest.mark.asyncio
    async def test_cancel_request_uses_reserved_admission_when_session_limit_is_full(
        self, monkeypatch
    ) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0, max_connections=1)
        proxy._active_connections = 1
        target = PGCancelTarget(
            proxy_process_id=1234,
            proxy_secret_key=5678,
            upstream_process_id=9012,
            upstream_secret_key=3456,
            upstream_host="db.internal",
            upstream_port=5432,
            connection_config={},
            source_id="warehouse",
        )
        proxy._cancel_targets[(1234, 5678)] = target
        routed = AsyncMock()
        monkeypatch.setattr(proxy, "_send_upstream_cancel", routed)
        reader = _make_reader(_pack_cancel_request(1234, 5678))
        writer, buf = _make_writer()

        await proxy._handle_client(reader, writer)

        assert proxy.active_connections == 1
        assert bytes(buf) == b""
        routed.assert_awaited_once_with(target)

    @pytest.mark.asyncio
    async def test_main_loop_rejects_oversized_client_frame(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0, max_message_bytes=8)
        raw = b"Q" + struct.pack("!I", 4 + 9) + b"x" * 9
        client_reader = _make_reader(raw)
        client_writer, client_buf = _make_writer()
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(client_reader, client_writer, upstream_reader, upstream_writer)

        assert upstream_buf == bytearray()
        assert b"exceeds limit 8" in bytes(client_buf)

    @pytest.mark.asyncio
    async def test_main_loop_frame_timeout_returns_error(self, monkeypatch) -> None:
        async def _slow_message(_reader, **_kwargs):
            await asyncio.sleep(1)

        monkeypatch.setattr("interlock.gateway.pg_proxy.read_message", _slow_message)
        proxy = PGProxy(listen_port=0, upstream_port=0, frame_timeout_seconds=0.001)
        client_reader = _make_reader(b"")
        client_writer, client_buf = _make_writer()
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(client_reader, client_writer, upstream_reader, upstream_writer)

        assert upstream_buf == bytearray()
        assert b"client frame timed out" in bytes(client_buf)

    @pytest.mark.asyncio
    async def test_gssenc_request_is_denied_and_closed(self) -> None:
        gssenc_request_code = 80877104
        raw = struct.pack("!II", 8, gssenc_request_code)
        proxy = PGProxy(listen_port=0, upstream_port=0)
        reader = _make_reader(raw)
        writer, buf = _make_writer()

        result = await proxy._handle_startup(reader, writer)

        assert result is None
        assert bytes(buf) == b"N"

    @pytest.mark.asyncio
    async def test_cancel_request_routes_known_proxy_key_and_closes_silently(
        self, monkeypatch
    ) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        target = PGCancelTarget(
            proxy_process_id=1234,
            proxy_secret_key=5678,
            upstream_process_id=9012,
            upstream_secret_key=3456,
            upstream_host="db.internal",
            upstream_port=5432,
            connection_config={"sslmode": "require"},
            source_id="warehouse",
        )
        proxy._cancel_targets[(1234, 5678)] = target
        routed = AsyncMock()
        monkeypatch.setattr(proxy, "_send_upstream_cancel", routed)
        reader = _make_reader(_pack_cancel_request(1234, 5678))
        writer, buf = _make_writer()

        result = await proxy._handle_startup(reader, writer)

        assert result is None
        assert bytes(buf) == b""
        routed.assert_awaited_once_with(target)

    @pytest.mark.asyncio
    async def test_cancel_request_with_unknown_or_stale_key_is_silent(self, monkeypatch) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        routed = AsyncMock()
        monkeypatch.setattr(proxy, "_send_upstream_cancel", routed)
        reader = _make_reader(_pack_cancel_request(1234, 5678))
        writer, buf = _make_writer()

        result = await proxy._handle_startup(reader, writer)

        assert result is None
        assert bytes(buf) == b""
        routed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_cancel_request_is_silent_and_not_routed(self, monkeypatch) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        routed = AsyncMock()
        monkeypatch.setattr(proxy, "_send_upstream_cancel", routed)
        raw = struct.pack("!III", 12, CANCEL_REQUEST_CODE, 1234)
        reader = _make_reader(raw)
        writer, buf = _make_writer()

        result = await proxy._handle_startup(reader, writer)

        assert result is None
        assert bytes(buf) == b""
        routed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backend_key_is_replaced_and_registered_for_cancellation(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        target = PGCancelTarget(
            proxy_process_id=1234,
            proxy_secret_key=5678,
            upstream_host="db.internal",
            upstream_port=5432,
            connection_config={},
            source_id="warehouse",
        )
        upstream_key = struct.pack("!II", 9012, 3456)

        client_key = proxy._register_backend_key(target, upstream_key)

        assert client_key == struct.pack("!II", 1234, 5678)
        assert target.upstream_process_id == 9012
        assert target.upstream_secret_key == 3456
        assert proxy._cancel_targets[(1234, 5678)] is target
        assert client_key != upstream_key

    @pytest.mark.asyncio
    async def test_upstream_cancel_uses_original_route_tls_and_backend_key(
        self, monkeypatch
    ) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0, startup_timeout_seconds=1)
        upstream_writer, upstream_buf = _make_writer()
        upstream_reader = _make_reader(b"")
        open_connection = AsyncMock(return_value=(upstream_reader, upstream_writer))
        monkeypatch.setattr("interlock.gateway.pg_proxy._open_upstream_connection", open_connection)
        target = PGCancelTarget(
            proxy_process_id=1234,
            proxy_secret_key=5678,
            upstream_process_id=9012,
            upstream_secret_key=3456,
            upstream_host="db.internal",
            upstream_port=6432,
            connection_config={"sslmode": "verify-full", "ssl_ca_ref": "env://PG_CA"},
            source_id="warehouse",
        )

        await proxy._send_upstream_cancel(target)

        open_connection.assert_awaited_once_with(
            "db.internal",
            6432,
            target.connection_config,
            allow_insecure_tls=True,
        )
        assert bytes(upstream_buf) == _pack_cancel_request(9012, 3456)
        upstream_writer.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_mapping_is_removed_with_original_session(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        target = PGCancelTarget(
            proxy_process_id=1234,
            proxy_secret_key=5678,
            upstream_process_id=9012,
            upstream_secret_key=3456,
            upstream_host="db.internal",
            upstream_port=5432,
            connection_config={},
            source_id="warehouse",
        )
        proxy._cancel_targets[(1234, 5678)] = target

        proxy._unregister_cancel_target(target)

        assert (1234, 5678) not in proxy._cancel_targets

    @pytest.mark.asyncio
    async def test_original_session_registers_rewrites_and_cleans_cancel_key(
        self, monkeypatch
    ) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        target = PGCancelTarget(
            proxy_process_id=1234,
            proxy_secret_key=5678,
            upstream_host="db.internal",
            upstream_port=5432,
            connection_config={},
            source_id="default",
        )
        monkeypatch.setattr(proxy, "_new_cancel_target", MagicMock(return_value=target))
        upstream_key = struct.pack("!II", 9012, 3456)
        upstream_reader = _make_reader(
            _pack_auth_ok() + pack_message("K", upstream_key) + _pack_ready_for_query()
        )
        upstream_writer, upstream_buf = _make_writer()
        open_connection = AsyncMock(return_value=(upstream_reader, upstream_writer))
        monkeypatch.setattr("interlock.gateway.pg_proxy._open_upstream_connection", open_connection)
        client_reader = _make_reader(_pack_startup() + pack_message(MSG_TYPE_TERMINATE, b""))
        client_writer, client_buf = _make_writer()

        await proxy._handle_client(client_reader, client_writer)

        assert pack_message("K", struct.pack("!II", 1234, 5678)) in bytes(client_buf)
        assert pack_message("K", upstream_key) not in bytes(client_buf)
        assert pack_message(MSG_TYPE_TERMINATE, b"") in bytes(upstream_buf)
        assert (1234, 5678) not in proxy._cancel_targets
        assert proxy.active_connections == 0

    @pytest.mark.asyncio
    async def test_collect_silent_rejects_oversized_result(self) -> None:
        response = _pack_command_complete("SELECT 1") + _pack_ready_for_query()
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            max_result_bytes=len(_pack_command_complete("SELECT 1")) - 1,
        )
        reader = _make_reader(response)

        with pytest.raises(ValueError, match="exceeds"):
            await proxy._collect_silent(reader)

    @pytest.mark.asyncio
    async def test_copy_data_message_rejected_before_upstream(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        client_reader = _make_reader(pack_message("d", b"copy bytes"))
        client_writer, client_buf = _make_writer()
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(client_reader, client_writer, upstream_reader, upstream_writer)

        assert upstream_buf == bytearray()
        assert b"COPY is not supported" in bytes(client_buf)

    @pytest.mark.asyncio
    async def test_simple_copy_sql_rejected_before_upstream(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            b"COPY users TO STDOUT\x00",
            client_writer,
            upstream_reader,
            upstream_writer,
        )

        assert upstream_buf == bytearray()
        assert b"COPY is not supported" in bytes(client_buf)

    @pytest.mark.asyncio
    async def test_close_statement_removes_prepared_statement_before_execute(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        execute = _pack_execute("portal")
        client_reader = _make_reader(
            _pack_parse("stmt", "SELECT 1")
            + _pack_close_statement("stmt")
            + _pack_bind("portal", "stmt")
            + execute
            + pack_message(MSG_TYPE_TERMINATE, b"")
        )
        upstream_reader = _make_reader(
            pack_message("1", b"") + pack_message("3", b"") + pack_message("2", b"")
        )
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._main_loop(client_reader, client_writer, upstream_reader, upstream_writer)

        assert execute not in bytes(upstream_buf)
        assert b"Unresolved prepared statement" in bytes(client_buf)


class TestPGProxyStartupForwarding:
    """Test that startup messages are forwarded to upstream."""

    @pytest.mark.asyncio
    async def test_forward_until_ready(self) -> None:
        """Auth exchange forwarded until ReadyForQuery."""
        proxy = PGProxy(listen_port=0, upstream_port=0)

        auth_ok = _pack_auth_ok()
        rfq = _pack_ready_for_query()
        upstream_reader = _make_reader(auth_ok + rfq)
        client_reader = _make_reader(b"")  # no client auth needed for AuthOk
        client_writer, client_buf = _make_writer()
        upstream_writer, _ = _make_writer()

        result = await proxy._forward_until_ready(
            client_reader, client_writer, upstream_reader, upstream_writer
        )

        assert result is True
        # Client should have received both messages
        assert bytes(client_buf) == auth_ok + rfq

    @pytest.mark.asyncio
    async def test_transparent_auth_rewrites_backend_key_data(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        target = PGCancelTarget(
            proxy_process_id=1234,
            proxy_secret_key=5678,
            upstream_host="db.internal",
            upstream_port=5432,
            connection_config={},
            source_id="warehouse",
        )
        upstream_key = struct.pack("!II", 9012, 3456)
        upstream_reader = _make_reader(
            _pack_auth_ok() + pack_message("K", upstream_key) + _pack_ready_for_query()
        )
        client_reader = _make_reader(b"")
        client_writer, client_buf = _make_writer()
        upstream_writer, _ = _make_writer()

        result = await proxy._forward_until_ready(
            client_reader,
            client_writer,
            upstream_reader,
            upstream_writer,
            cancel_target=target,
        )

        assert result is True
        assert pack_message("K", struct.pack("!II", 1234, 5678)) in bytes(client_buf)
        assert pack_message("K", upstream_key) not in bytes(client_buf)


class TestPGProxySimpleQueryExtraction:
    """Test that SimpleQuery SQL is extracted correctly."""

    @pytest.mark.asyncio
    async def test_query_forwarded_no_cache(self) -> None:
        """Without caches, query goes to upstream and response comes back."""
        proxy = PGProxy(listen_port=0, upstream_port=0)

        # Build the query message payload (just the SQL+NUL, no type/length)
        sql_payload = b"SELECT 1\x00"

        # Build upstream response
        cmd_complete = _pack_command_complete("SELECT 1")
        rfq = _pack_ready_for_query()
        upstream_reader = _make_reader(cmd_complete + rfq)
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload, client_writer, upstream_reader, upstream_writer
        )

        # Upstream should have received the query
        assert len(upstream_buf) > 0
        assert chr(upstream_buf[0]) == MSG_TYPE_QUERY

        # Client should have received the response
        assert bytes(client_buf) == cmd_complete + rfq

    @pytest.mark.asyncio
    async def test_strict_audit_failure_prevents_simple_success_frames(self) -> None:
        audit = MagicMock()
        audit.log = AsyncMock(side_effect=AuditUnavailableError("spool unavailable"))
        proxy = PGProxy(listen_port=0, upstream_port=0, audit_logger=audit)
        upstream_reader = _make_reader(_pack_command_complete() + _pack_ready_for_query())
        upstream_writer, _ = _make_writer()
        client_writer, client_buf = _make_writer()

        with pytest.raises(AuditUnavailableError, match="spool unavailable"):
            await proxy._handle_simple_query(
                b"SELECT 1\x00",
                client_writer,
                upstream_reader,
                upstream_writer,
            )

        assert client_buf == bytearray()

    @pytest.mark.asyncio
    async def test_strict_audit_failure_prevents_extended_success_frames(self) -> None:
        audit = MagicMock()
        audit.log = AsyncMock(side_effect=AuditUnavailableError("spool unavailable"))
        proxy = PGProxy(listen_port=0, upstream_port=0, audit_logger=audit)
        upstream_reader = _make_reader(_pack_command_complete() + _pack_ready_for_query())
        client_writer, client_buf = _make_writer()
        execution = ExtendedExecutionContext(
            sql="SELECT 1",
            source_id="default",
            identity=None,
            operation="read",
            fingerprint=None,
            tables=[],
            started_at=time.monotonic(),
        )

        with pytest.raises(AuditUnavailableError, match="spool unavailable"):
            await proxy._handle_extended_sync_response(
                upstream_reader,
                client_writer,
                [execution],
            )

        assert client_buf == bytearray()

    @pytest.mark.asyncio
    async def test_strict_audit_failure_prevents_flush_success_frames(self) -> None:
        audit = MagicMock()
        audit.log = AsyncMock(side_effect=AuditUnavailableError("spool unavailable"))
        proxy = PGProxy(listen_port=0, upstream_port=0, audit_logger=audit)
        upstream_reader = _make_reader(_pack_command_complete())
        client_writer, client_buf = _make_writer()
        execution = ExtendedExecutionContext(
            sql="SELECT 1",
            source_id="default",
            identity=None,
            operation="read",
            fingerprint=None,
            tables=[],
            started_at=time.monotonic(),
        )

        with pytest.raises(AuditUnavailableError, match="spool unavailable"):
            await proxy._handle_extended_flush_response(
                upstream_reader,
                client_writer,
                [execution],
                [],
            )

        assert client_buf == bytearray()

    @pytest.mark.asyncio
    async def test_multistatement_write_is_rejected_before_upstream(self) -> None:
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            write_classifier=WriteClassifier(),
            approval_queue=MagicMock(),
        )
        sql_payload = b"SELECT 1; DROP TABLE customers\x00"
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload, client_writer, upstream_reader, upstream_writer
        )

        assert upstream_buf == bytearray()
        assert b"Write blocked" in bytes(client_buf)
        assert bytes(client_buf).endswith(_pack_ready_for_query())

    @pytest.mark.asyncio
    async def test_read_then_low_risk_write_is_not_cached_as_read(self) -> None:
        from interlock.cache.l1 import L1Cache

        l1 = L1Cache(max_size=100, ttl_seconds=60)
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            l1_cache=l1,
            write_classifier=WriteClassifier(),
        )
        sql_payload = b"SELECT 1; INSERT INTO logs (msg) VALUES ('ok')\x00"
        upstream_reader = _make_reader(
            _pack_command_complete("INSERT 0 1") + _pack_ready_for_query()
        )
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload,
            client_writer,
            upstream_reader,
            upstream_writer,
        )

        assert len(upstream_buf) > 0
        assert b"INSERT 0 1" in bytes(client_buf)
        assert l1.stats["size"] == 0

    @pytest.mark.asyncio
    async def test_multi_statement_governance_sees_every_statements_tables(self) -> None:
        """`SELECT 1; SELECT * FROM orders` used to reach governance with no tables,
        so a deny on `orders` never matched and the upstream answered with rows from
        the denied table. Governance must be asked about every table any statement
        reads - and a refusal must stop the message before the upstream sees it."""
        from interlock.gateway.pipeline import GatewayDecision

        proxy = PGProxy(listen_port=0, upstream_port=0, write_classifier=WriteClassifier())
        captured: dict[str, Any] = {}

        async def preflight(request: Any) -> GatewayDecision:
            captured["request"] = request
            return GatewayDecision(allowed=False, status_code=403, reason="denied for test")

        pipeline = MagicMock()
        pipeline.preflight = preflight
        pipeline.source_generation = AsyncMock(return_value=0)
        pipeline.audit = AsyncMock()
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        with patch.object(proxy, "_pipeline", return_value=pipeline):
            await proxy._handle_simple_query(
                b"SELECT 1; SELECT * FROM orders; SELECT id FROM customers\x00",
                client_writer,
                upstream_reader,
                upstream_writer,
            )

        assert captured["request"].tables == ["orders", "customers"]
        assert upstream_buf == bytearray()

    @pytest.mark.asyncio
    async def test_multi_statement_read_is_not_cached_under_first_statement(self) -> None:
        from interlock.cache.l1 import L1Cache

        l1 = L1Cache(max_size=100, ttl_seconds=60)
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            l1_cache=l1,
            write_classifier=WriteClassifier(),
        )
        sql_payload = b"SELECT 1; SELECT 2\x00"
        upstream_reader = _make_reader(_pack_command_complete("SELECT 2") + _pack_ready_for_query())
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload,
            client_writer,
            upstream_reader,
            upstream_writer,
        )

        assert len(upstream_buf) > 0
        assert b"SELECT 2" in bytes(client_buf)
        assert l1.stats["size"] == 0


class TestPGProxyCacheHit:
    """Test cache hit serves cached bytes without upstream."""

    @pytest.mark.asyncio
    async def test_l1_cache_hit(self) -> None:
        """L1 cache hit serves response directly, no upstream interaction."""
        from interlock.cache.l1 import L1Cache

        l1 = L1Cache(max_size=100, ttl_seconds=60)

        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            l1_cache=l1,
        )

        # Pre-populate cache with the key the proxy will compute. Since
        # this test invokes _handle_simple_query without an identity,
        # the proxy uses compute_cache_key with empty identity scope
        # (P0-C: cache keys include identity).
        cached_response = _pack_command_complete("SELECT 1") + _pack_ready_for_query()
        from interlock.core.normalizer import compute_cache_key, normalize_sql
        from interlock.gateway.pg_proxy import _decision_scope_hash
        from interlock.gateway.pipeline import GatewayDecision

        nq = normalize_sql("SELECT 1", "default")
        assert not isinstance(nq, list)
        key = compute_cache_key(
            protocol="postgresql",
            source_id="default",
            normalized_sql=nq.normalized_sql or "",
            parameters=nq.parameters,
            policy_scope_hash=_decision_scope_hash(
                GatewayDecision(allowed=True, redaction_required=True)
            ),
        )
        await l1.put(key, cached_response)

        sql_payload = b"SELECT 1\x00"
        # No upstream data needed since we should hit cache
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload, client_writer, upstream_reader, upstream_writer
        )

        # Upstream should NOT have received anything
        assert len(upstream_buf) == 0
        # Client should have received the cached response
        assert bytes(client_buf) == cached_response

    @pytest.mark.asyncio
    async def test_strict_audit_failure_prevents_cached_success_frames(self) -> None:
        audit = MagicMock()
        audit.log = AsyncMock(side_effect=AuditUnavailableError("spool unavailable"))
        proxy = PGProxy(listen_port=0, upstream_port=0, audit_logger=audit)
        cached_response = _pack_command_complete() + _pack_ready_for_query()
        proxy._cache_get = AsyncMock(return_value=(cached_response, "l1", {}))  # type: ignore[method-assign]
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        with pytest.raises(AuditUnavailableError, match="spool unavailable"):
            await proxy._handle_simple_query(
                b"SELECT 1\x00",
                client_writer,
                upstream_reader,
                upstream_writer,
            )

        assert upstream_buf == bytearray()
        assert client_buf == bytearray()


class TestCachedEntriesFromAnotherProtocol:
    """A cached entry that is not PostgreSQL wire format is never written to a client.

    An independent evaluation of rc.13 found MCP's JSON under the key a
    `psql` read used; the proxy wrote it to the socket and `psql` lost
    synchronization. The key now separates protocols, and this check makes any
    future collision a cache miss instead of a broken connection.
    """

    def test_a_complete_response_is_recognised(self) -> None:
        from interlock.gateway.pg_proxy import is_simple_query_response

        assert is_simple_query_response(_pack_command_complete() + _pack_ready_for_query())

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b'[{"id": 1, "name": "Ada"}]',
            b"not a response at all",
            _pack_command_complete(),  # no ReadyForQuery at the end
            (_pack_command_complete() + _pack_ready_for_query())[:-1],  # truncated
        ],
        ids=["empty", "mcp-json", "text", "no-ready-for-query", "truncated"],
    )
    def test_anything_else_is_refused(self, data: bytes) -> None:
        from interlock.gateway.pg_proxy import is_simple_query_response

        assert not is_simple_query_response(data)

    @pytest.mark.asyncio
    async def test_an_mcp_entry_is_a_miss_and_the_query_goes_upstream(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        proxy._cache_get = AsyncMock(  # type: ignore[method-assign]
            return_value=(b'[{"id": 1, "name": "Ada"}]', "l2", {})
        )
        upstream_response = _pack_command_complete("SELECT 1") + _pack_ready_for_query()
        upstream_reader = _make_reader(upstream_response)
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            b"SELECT 1\x00", client_writer, upstream_reader, upstream_writer
        )

        assert len(upstream_buf) > 0, "the foreign entry was served instead of going upstream"
        assert b"[{" not in bytes(client_buf)
        assert bytes(client_buf) == upstream_response


class TestPGProxyCacheMiss:
    """Test cache miss forwards to upstream and stores in cache."""

    @pytest.mark.asyncio
    async def test_l1_cache_populated_after_miss(self) -> None:
        """After a cache miss, the response is stored in L1."""
        from interlock.cache.l1 import L1Cache

        l1 = L1Cache(max_size=100, ttl_seconds=60)

        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            l1_cache=l1,
        )

        sql_payload = b"SELECT 42\x00"
        cached_response = _pack_command_complete("SELECT 1") + _pack_ready_for_query()
        upstream_reader = _make_reader(cached_response)
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload, client_writer, upstream_reader, upstream_writer
        )

        # Now check that L1 has the response under the identity-aware key.
        from interlock.core.normalizer import compute_cache_key, normalize_sql
        from interlock.gateway.pg_proxy import _decision_scope_hash
        from interlock.gateway.pipeline import GatewayDecision

        nq = normalize_sql("SELECT 42", "default")
        assert not isinstance(nq, list)
        key = compute_cache_key(
            protocol="postgresql",
            source_id="default",
            normalized_sql=nq.normalized_sql or "",
            parameters=nq.parameters,
            policy_scope_hash=_decision_scope_hash(
                GatewayDecision(allowed=True, redaction_required=True)
            ),
        )
        result = await l1.get(key)
        assert result.hit is True
        assert result.data == cached_response

    @pytest.mark.asyncio
    async def test_cache_put_records_source_table_dependency(self) -> None:
        """PG read cache writes record dependency dimensions for later invalidation."""
        l1 = AsyncMock()
        l2 = AsyncMock()
        invalidator = AsyncMock()
        proxy = PGProxy(l1_cache=l1, l2_cache=l2, cache_invalidator=invalidator)

        await proxy._cache_put(
            "fingerprint-1",
            b"response",
            source_id="src1",
            tables=["customers"],
        )

        # The entry carries the redaction applied to it, so a hit can audit it.
        stored = with_redaction(
            {"source_id": "src1", "protocol": "pg", "tables": ["customers"]},
            pii_detected=False,
            pii_types=[],
        )
        l1.put.assert_awaited_once_with("fingerprint-1", b"response", stored)
        l2.put.assert_awaited_once_with("fingerprint-1", b"response", metadata=stored)
        invalidator.record_dependency.assert_awaited_once_with(
            "fingerprint-1",
            source_id="src1",
            tables=["customers"],
            protocol="pg",
            metadata={
                "source_id": "src1",
                "protocol": "pg",
                "tables": ["customers"],
            },
        )

    @pytest.mark.asyncio
    async def test_write_invalidation_uses_cache_invalidator(self) -> None:
        """PG writes delegate dependency invalidation to CacheInvalidator."""
        l1 = AsyncMock()
        l2 = AsyncMock()
        invalidator = AsyncMock()
        proxy = PGProxy(
            l1_cache=l1,
            l2_cache=l2,
            cache_invalidator=invalidator,
        )

        await proxy._invalidate_caches_after_write("src1", ["customers"])

        invalidator.invalidate_for_tables.assert_awaited_once_with("src1", ["customers"])
        l1.clear.assert_not_awaited()
        l2.clear.assert_not_awaited()


class TestPGProxyMainLoop:
    """Test the main message loop dispatching."""

    @pytest.mark.asyncio
    async def test_terminate_exits_loop(self) -> None:
        """Receiving a Terminate message should forward it and exit."""
        proxy = PGProxy(listen_port=0, upstream_port=0)

        terminate_msg = pack_message(MSG_TYPE_TERMINATE, b"")
        client_reader = _make_reader(terminate_msg)
        client_writer, _ = _make_writer()
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(client_reader, client_writer, upstream_reader, upstream_writer)

        # Upstream should have received the Terminate message
        assert len(upstream_buf) > 0
        assert chr(upstream_buf[0]) == MSG_TYPE_TERMINATE

    @pytest.mark.asyncio
    async def test_transparent_passthrough(self) -> None:
        """Unknown message types are forwarded transparently."""
        proxy = PGProxy(listen_port=0, upstream_port=0)

        # Send a Bind message ('B') followed by Terminate so the loop exits.
        # Upstream needs a BindComplete ('2') response for the drain to find.
        bind_payload = b"\x00\x00\x00\x00\x00\x00"
        bind_msg = pack_message("B", bind_payload)
        terminate_msg = pack_message(MSG_TYPE_TERMINATE, b"")

        bind_complete = pack_message("2", b"")

        client_reader = _make_reader(bind_msg + terminate_msg)
        client_writer, _ = _make_writer()
        upstream_reader = _make_reader(bind_complete)
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(client_reader, client_writer, upstream_reader, upstream_writer)

        # Upstream should have the bind message + terminate
        assert chr(upstream_buf[0]) == "B"

    @pytest.mark.asyncio
    async def test_pipelined_parse_describe_flush_does_not_deadlock(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        client_reader = _make_reader(
            _pack_parse("stmt", "SELECT 1")
            + _pack_describe_statement("stmt")
            + pack_message("H", b"")
            + pack_message(MSG_TYPE_TERMINATE, b"")
        )
        client_writer, client_buf = _make_writer()
        upstream_reader = _make_reader(
            pack_message("1", b"")
            + pack_message("t", struct.pack("!H", 0))
            + pack_message("n", b"")
        )
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(
            client_reader,
            client_writer,
            upstream_reader,
            upstream_writer,
        )

        assert bytes(client_buf) == (
            pack_message("1", b"")
            + pack_message("t", struct.pack("!H", 0))
            + pack_message("n", b"")
        )
        assert pack_message("H", b"") in bytes(upstream_buf)

    @pytest.mark.asyncio
    async def test_extended_prepared_dangerous_write_is_rejected_before_upstream(
        self,
    ) -> None:
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            write_classifier=WriteClassifier(),
        )
        sql = "WITH del AS (DELETE FROM customers RETURNING *) SELECT * FROM del"
        client_reader = _make_reader(
            _pack_parse("danger", sql)
            + _pack_bind("portal", "danger")
            + _pack_execute("portal")
            + pack_message(MSG_TYPE_TERMINATE, b"")
        )
        client_writer, client_buf = _make_writer()
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(client_reader, client_writer, upstream_reader, upstream_writer)

        assert bytes(upstream_buf) == pack_message(MSG_TYPE_TERMINATE, b"")
        assert bytes(client_buf).count(_pack_ready_for_query()) == 3
        assert b"DELETE" not in bytes(upstream_buf)
        assert b"Rejected prepared statement" in bytes(client_buf)


class TestPGProxyLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        """Proxy can start and stop without errors."""
        proxy = PGProxy(
            listen_host="127.0.0.1",
            listen_port=0,  # OS-assigned port
            upstream_port=0,
        )
        await proxy.start()
        assert proxy._server is not None
        assert proxy.active_connections == 0

        await proxy.stop()
        assert proxy._server is None


class TestUpstreamConnectionFailuresReachTheClient:
    """A client InterLock cannot connect upstream must be told why, not dropped.

    `_open_upstream_connection` runs after client authentication and before
    AuthenticationOk, where a FATAL ErrorResponse is still valid. Its failures
    used to escape to `_handle_client`'s ConnectionError handler, which logged
    the reason and closed the socket, so the client saw only a lost
    connection.
    """

    @pytest.mark.asyncio
    async def test_a_refused_upstream_is_a_fatal_08004_carrying_the_reason(
        self, monkeypatch
    ) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        refusal = UpstreamConnectionRefusedError("Verified upstream PostgreSQL TLS is required")
        monkeypatch.setattr(
            "interlock.gateway.pg_proxy._open_upstream_connection",
            AsyncMock(side_effect=refusal),
        )
        client_reader = _make_reader(_pack_startup())
        client_writer, client_buf = _make_writer()

        await proxy._handle_client(client_reader, client_writer)

        sent = bytes(client_buf)
        assert b"SFATAL\x00" in sent
        assert b"C08004\x00" in sent
        assert b"Verified upstream PostgreSQL TLS is required" in sent
        assert proxy.active_connections == 0

    @pytest.mark.asyncio
    async def test_an_unreachable_upstream_is_a_fatal_08006_without_its_address(
        self, monkeypatch
    ) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        monkeypatch.setattr(
            "interlock.gateway.pg_proxy._open_upstream_connection",
            AsyncMock(side_effect=ConnectionRefusedError("Connect call failed ('10.0.0.5', 5432)")),
        )
        client_reader = _make_reader(_pack_startup())
        client_writer, client_buf = _make_writer()

        await proxy._handle_client(client_reader, client_writer)

        sent = bytes(client_buf)
        assert b"SFATAL\x00" in sent
        assert b"C08006\x00" in sent
        assert b"10.0.0.5" not in sent
        assert proxy.active_connections == 0

    @pytest.mark.asyncio
    async def test_the_upstream_open_raises_its_refusals_as_client_safe_errors(
        self, monkeypatch
    ) -> None:
        with pytest.raises(UpstreamConnectionRefusedError, match="Verified upstream"):
            await _open_upstream_connection(
                "db.internal", 5432, {"sslmode": "require"}, allow_insecure_tls=False
            )

        reader = _make_reader(b"N")
        writer, _ = _make_writer()

        async def _open_connection(_host: str, _port: int):
            return reader, writer

        monkeypatch.setattr(asyncio, "open_connection", _open_connection)
        with pytest.raises(UpstreamConnectionRefusedError, match="refused SSL"):
            await _open_upstream_connection("db.internal", 5432, {"sslmode": "verify-full"})
