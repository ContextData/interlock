"""Integration tests for PG proxy auth, policy, write classifier, and approval queue wiring."""

from __future__ import annotations

import asyncio
import ssl
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.cache.l1 import L1Cache
from interlock.core.write_classifier import WriteClassifier
from interlock.errors import WriteApprovalRequiredError
from interlock.gateway.pg_messages import (
    MSG_TYPE_COMMAND_COMPLETE,
    MSG_TYPE_PARSE,
    MSG_TYPE_QUERY,
    MSG_TYPE_READY_FOR_QUERY,
    MSG_TYPE_TERMINATE,
    PROTOCOL_VERSION_3_0,
    pack_message,
)
from interlock.gateway.pg_proxy import (
    PGCancelTarget,
    PGProxy,
    _build_error_response,
    _build_upstream_startup,
    _classify_operation,
    _extract_startup_user,
    _open_upstream_connection,
    _pg_md5_password,
    _resolve_upstream_host_port,
)
from interlock.models import (
    AgentType,
    IdentityContext,
    PolicyDecision,
    RiskLevel,
)
from interlock.pipeline.pii_fast import PIIFastScanner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _make_writer() -> tuple[asyncio.StreamWriter, bytearray]:
    buf = bytearray()
    transport = MagicMock()
    transport.is_closing.return_value = False
    protocol = MagicMock()
    writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_event_loop())

    def _capture_write(data: bytes | bytearray | memoryview) -> None:
        buf.extend(data)

    writer.write = _capture_write  # type: ignore[assignment]
    writer.drain = AsyncMock()  # type: ignore[method-assign]
    writer.close = MagicMock()  # type: ignore[method-assign]
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))  # type: ignore[method-assign]
    return writer, buf


def _pack_startup(user: str = "test", version: int = PROTOCOL_VERSION_3_0) -> bytes:
    payload = struct.pack("!I", version) + f"user\x00{user}\x00\x00".encode()
    length = 4 + len(payload)
    return struct.pack("!I", length) + payload


def _pack_query(sql: str) -> bytes:
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
    return pack_message(MSG_TYPE_PARSE, payload)


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


def _pack_execute(portal_name: str = "") -> bytes:
    return pack_message("E", portal_name.encode("utf-8") + b"\x00" + struct.pack("!I", 0))


def _pack_sync() -> bytes:
    return pack_message("S", b"")


def _pack_data_row(*fields: str) -> bytes:
    payload = struct.pack("!H", len(fields))
    for field in fields:
        raw = field.encode("utf-8")
        payload += struct.pack("!i", len(raw)) + raw
    return pack_message("D", payload)


def _pack_ready_for_query(status: str = "I") -> bytes:
    return pack_message(MSG_TYPE_READY_FOR_QUERY, status.encode("ascii"))


def _pack_command_complete(tag: str = "SELECT 1") -> bytes:
    return pack_message(MSG_TYPE_COMMAND_COMPLETE, tag.encode("utf-8") + b"\x00")


class _AuditCollector:
    def __init__(self) -> None:
        self.records = []

    async def log(self, record) -> None:
        self.records.append(record)


def _identity(
    user: str = "testuser",
    identity_id: int = 1,
    roles: list[str] | None = None,
) -> IdentityContext:
    return IdentityContext(
        identity_id=identity_id,
        user=user,
        agent_type=AgentType.CUSTOM,
        team="eng",
        roles=roles or ["reader"],
        session_id="sess-001",
    )


# =========================================================================
# _extract_startup_user
# =========================================================================


class TestExtractStartupUser:
    """Test startup message user extraction."""

    def test_extracts_user(self) -> None:
        data = _pack_startup(user="alice")
        assert _extract_startup_user(data) == "alice"

    def test_extracts_user_with_extra_params(self) -> None:
        payload = (
            struct.pack("!I", PROTOCOL_VERSION_3_0) + b"user\x00bob\x00database\x00mydb\x00\x00"
        )
        length = 4 + len(payload)
        data = struct.pack("!I", length) + payload
        assert _extract_startup_user(data) == "bob"

    def test_no_user_returns_none(self) -> None:
        payload = struct.pack("!I", PROTOCOL_VERSION_3_0) + b"database\x00mydb\x00\x00"
        length = 4 + len(payload)
        data = struct.pack("!I", length) + payload
        assert _extract_startup_user(data) is None

    def test_short_data_returns_none(self) -> None:
        assert _extract_startup_user(b"\x00\x00") is None


# =========================================================================
# Upstream service credential resolution
# =========================================================================


class TestUpstreamServiceCredentials:
    """PG proxy should route/auth upstream with source service credentials."""

    def test_build_upstream_startup_overrides_client_user_and_database(self) -> None:
        startup = _pack_startup(user="agent")
        raw, upstream_user, upstream_password = _build_upstream_startup(
            startup,
            {
                "user": "svc_user",
                "password": "svc_pass",
                "database": "customer_db",
            },
            "agent",
            "agent",
        )

        assert upstream_user == "svc_user"
        assert upstream_password == "svc_pass"
        assert b"user\x00svc_user\x00" in raw
        assert b"database\x00customer_db\x00" in raw

    def test_build_upstream_startup_strips_authorization_sensitive_params(self) -> None:
        payload = struct.pack("!I", PROTOCOL_VERSION_3_0)
        for key, value in {
            "user": "agent",
            "database": "e2e_pg",
            "application_name": "psql",
            "options": "-c search_path=private -c role=owner",
            "search_path": "private",
            "role": "owner",
        }.items():
            payload += key.encode("utf-8") + b"\x00" + value.encode("utf-8") + b"\x00"
        payload += b"\x00"
        startup = struct.pack("!I", 4 + len(payload)) + payload

        raw, upstream_user, upstream_password = _build_upstream_startup(
            startup,
            {
                "user": "svc_user",
                "password": "svc_pass",
                "database": "customer_db",
            },
            "agent",
            "e2e_pg",
        )

        assert upstream_user == "svc_user"
        assert upstream_password == "svc_pass"
        assert b"user\x00svc_user\x00" in raw
        assert b"database\x00customer_db\x00" in raw
        assert b"application_name\x00psql\x00" in raw
        assert b"options\x00" not in raw
        assert b"search_path\x00" not in raw
        assert b"role\x00owner\x00" not in raw

    def test_upstream_host_port_can_come_from_secret_refs(self, monkeypatch) -> None:
        monkeypatch.setenv("UPSTREAM_HOST", "db.internal")
        monkeypatch.setenv("UPSTREAM_PORT", "15432")

        host, port = _resolve_upstream_host_port(
            {"host_ref": "env://UPSTREAM_HOST", "port_ref": "env://UPSTREAM_PORT"},
            "fallback",
            5432,
        )

        assert host == "db.internal"
        assert port == 15432

    def test_md5_password_matches_postgres_format(self) -> None:
        encoded = _pg_md5_password("secret", "svc_user", b"1234")
        assert encoded.startswith(b"md5")
        assert len(encoded) == 35

    @pytest.mark.asyncio
    async def test_upstream_tls_verifies_certificates_by_default(self, monkeypatch) -> None:
        reader = _make_reader(b"S")
        writer, _ = _make_writer()
        writer.start_tls = AsyncMock()  # type: ignore[method-assign]

        async def _open_connection(host: str, port: int):
            assert host == "db.example.com"
            assert port == 5432
            return reader, writer

        monkeypatch.setattr(asyncio, "open_connection", _open_connection)

        await _open_upstream_connection("db.example.com", 5432, {"ssl": "require"})

        context = writer.start_tls.call_args.args[0]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert writer.start_tls.call_args.kwargs["server_hostname"] == "db.example.com"

    @pytest.mark.asyncio
    async def test_upstream_tls_verification_can_be_explicitly_disabled(self, monkeypatch) -> None:
        reader = _make_reader(b"S")
        writer, _ = _make_writer()
        writer.start_tls = AsyncMock()  # type: ignore[method-assign]

        async def _open_connection(_host: str, _port: int):
            return reader, writer

        monkeypatch.setattr(asyncio, "open_connection", _open_connection)

        await _open_upstream_connection(
            "db.internal",
            5432,
            {"ssl": "require", "ssl_verify": False},
        )

        context = writer.start_tls.call_args.args[0]
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False

    @pytest.mark.asyncio
    async def test_production_rejects_unverified_upstream_tls(self, monkeypatch) -> None:
        reader = _make_reader(b"S")
        writer, _ = _make_writer()

        async def _open_connection(_host: str, _port: int):
            return reader, writer

        monkeypatch.setattr(asyncio, "open_connection", _open_connection)

        with pytest.raises(ConnectionError, match="Unverified upstream"):
            await _open_upstream_connection(
                "db.internal",
                5432,
                {"ssl": "verify-full", "ssl_verify": False},
                allow_insecure_tls=False,
            )


# =========================================================================
# _build_error_response
# =========================================================================


class TestBuildErrorResponse:
    """Test PG ErrorResponse message format."""

    def test_basic_format(self) -> None:
        raw = _build_error_response("ERROR", "42501", "Access denied")
        # First byte should be 'E'
        assert chr(raw[0]) == "E"
        # Should contain severity, code, and message fields
        assert b"SERROR\x00" in raw
        assert b"C42501\x00" in raw
        assert b"MAccess denied\x00" in raw
        # Should end with terminator NUL before final length check
        # The payload inside should end with \x00 (field terminator)

    def test_fatal_severity(self) -> None:
        raw = _build_error_response("FATAL", "28P01", "Auth failed")
        assert b"SFATAL\x00" in raw
        assert b"MAuth failed\x00" in raw


# =========================================================================
# _classify_operation
# =========================================================================


class TestClassifyOperation:
    """Test operation type classification."""

    def test_select_is_read(self) -> None:
        assert _classify_operation("SELECT", "SELECT 1") == "read"

    def test_insert_is_write(self) -> None:
        assert _classify_operation("INSERT", "INSERT INTO t VALUES (1)") == "write"

    def test_delete_is_write(self) -> None:
        assert _classify_operation("DELETE", "DELETE FROM t") == "write"

    def test_drop_is_write(self) -> None:
        assert _classify_operation("DROP", "DROP TABLE t") == "write"

    def test_fallback_from_sql(self) -> None:
        assert _classify_operation(None, "  UPDATE t SET x=1") == "write"

    def test_fallback_select(self) -> None:
        assert _classify_operation(None, "SELECT 1") == "read"


# =========================================================================
# Backward compatibility - proxy works without auth/policy
# =========================================================================


class TestProxyBackwardCompat:
    """Proxy must still work when auth/policy/write_classifier are None."""

    @pytest.mark.asyncio
    async def test_simple_query_no_auth(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)

        sql_payload = b"SELECT 1\x00"
        cmd = _pack_command_complete("SELECT 1")
        rfq = _pack_ready_for_query()
        upstream_reader = _make_reader(cmd + rfq)
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload, client_writer, upstream_reader, upstream_writer
        )

        assert len(upstream_buf) > 0
        assert bytes(client_buf) == cmd + rfq

    @pytest.mark.asyncio
    async def test_main_loop_terminate_no_auth(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)

        terminate_msg = pack_message(MSG_TYPE_TERMINATE, b"")
        client_reader = _make_reader(terminate_msg)
        client_writer, _ = _make_writer()
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(client_reader, client_writer, upstream_reader, upstream_writer)

        assert len(upstream_buf) > 0
        assert chr(upstream_buf[0]) == MSG_TYPE_TERMINATE


# =========================================================================
# InterLock-owned PG auth
# =========================================================================


class TestInterLockPgAuth:
    """Client auth is resolved by InterLock before upstream service auth."""

    @pytest.mark.asyncio
    async def test_client_password_auth_uses_authenticate_pg(self) -> None:
        auth = MagicMock()
        identity = _identity(user="agent")
        auth.authenticate_pg = AsyncMock(return_value=identity)
        proxy = PGProxy(listen_port=0, upstream_port=0, auth_manager=auth)

        client_reader = _make_reader(pack_message("p", b"agent-secret\x00"))
        client_writer, client_buf = _make_writer()

        resolved = await proxy._authenticate_pg_client(
            client_reader,
            client_writer,
            "agent",
        )

        assert resolved == identity
        auth.authenticate_pg.assert_awaited_once_with("agent", "agent-secret")
        assert bytes(client_buf).startswith(b"R")
        assert struct.unpack(">I", bytes(client_buf)[5:9])[0] == 3

    @pytest.mark.asyncio
    async def test_upstream_cleartext_auth_uses_service_password(self) -> None:
        proxy = PGProxy(listen_port=0, upstream_port=0)
        upstream_reader = _make_reader(
            pack_message("R", struct.pack("!I", 3))
            + pack_message("R", struct.pack("!I", 0))
            + _pack_ready_for_query()
        )
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        result = await proxy._complete_upstream_auth(
            client_writer,
            upstream_reader,
            upstream_writer,
            upstream_user="svc_user",
            upstream_password="svc_pass",
        )

        assert result is True
        assert bytes(upstream_buf) == pack_message("p", b"svc_pass\x00")
        assert pack_message("R", struct.pack("!I", 0)) in bytes(client_buf)
        assert bytes(client_buf).endswith(_pack_ready_for_query())

    @pytest.mark.asyncio
    async def test_upstream_service_auth_rewrites_backend_key_data(self) -> None:
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
            pack_message("R", struct.pack("!I", 0))
            + pack_message("K", upstream_key)
            + _pack_ready_for_query()
        )
        upstream_writer, _ = _make_writer()
        client_writer, client_buf = _make_writer()

        result = await proxy._complete_upstream_auth(
            client_writer,
            upstream_reader,
            upstream_writer,
            upstream_user="svc_user",
            upstream_password="svc_pass",
            cancel_target=target,
        )

        assert result is True
        assert pack_message("K", struct.pack("!II", 1234, 5678)) in bytes(client_buf)
        assert pack_message("K", upstream_key) not in bytes(client_buf)


# =========================================================================
# Policy deny returns error
# =========================================================================


class TestPolicyDeny:
    """Test that policy engine denial sends an error to the client."""

    @pytest.mark.asyncio
    async def test_read_query_denied(self) -> None:
        policy = MagicMock()
        policy.evaluate.return_value = PolicyDecision(
            allowed=False,
            reason="No matching policy rule - default deny",
        )

        proxy = PGProxy(listen_port=0, upstream_port=0, policy_engine=policy)

        identity = _identity()
        sql_payload = b"SELECT * FROM secrets\x00"

        # Upstream should NOT be contacted
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload,
            client_writer,
            upstream_reader,
            upstream_writer,
            identity=identity,
        )

        # Upstream should have received nothing
        assert len(upstream_buf) == 0

        # Client should have received ErrorResponse + ReadyForQuery
        response = bytes(client_buf)
        assert chr(response[0]) == "E"  # ErrorResponse
        assert b"Policy denied" in response
        # Should end with ReadyForQuery
        assert b"Z" in response

    @pytest.mark.asyncio
    async def test_read_query_allowed(self) -> None:
        policy = MagicMock()
        policy.evaluate.return_value = PolicyDecision(
            allowed=True,
            rule_id=1,
            rule_name="allow-all-reads",
            reason="Matched rule 'allow-all-reads'",
        )

        proxy = PGProxy(listen_port=0, upstream_port=0, policy_engine=policy)

        identity = _identity()
        sql_payload = b"SELECT 1\x00"

        cmd = _pack_command_complete("SELECT 1")
        rfq = _pack_ready_for_query()
        upstream_reader = _make_reader(cmd + rfq)
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload,
            client_writer,
            upstream_reader,
            upstream_writer,
            identity=identity,
        )

        # Upstream should have received the query
        assert len(upstream_buf) > 0
        assert chr(upstream_buf[0]) == MSG_TYPE_QUERY

        # Client should have received the response
        assert bytes(client_buf) == cmd + rfq


# =========================================================================
# Write classifier blocks EXECUTE
# =========================================================================


class TestWriteClassifierBlocked:
    """Test that EXECUTE/DO blocks are rejected."""

    @pytest.mark.asyncio
    async def test_execute_blocked(self) -> None:
        wc = WriteClassifier()

        proxy = PGProxy(listen_port=0, upstream_port=0, write_classifier=wc)

        sql_payload = b"EXECUTE my_plan\x00"

        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload,
            client_writer,
            upstream_reader,
            upstream_writer,
        )

        # Upstream should have received nothing
        assert len(upstream_buf) == 0

        response = bytes(client_buf)
        assert chr(response[0]) == "E"
        assert b"not permitted" in response


# =========================================================================
# Write classifier queues DROP TABLE
# =========================================================================


class TestWriteClassifierQueued:
    """Test that DROP TABLE is queued for approval."""

    @pytest.mark.asyncio
    async def test_drop_table_queued(self) -> None:
        wc = WriteClassifier()

        approval_queue = AsyncMock()
        approval_queue.submit = AsyncMock(
            side_effect=WriteApprovalRequiredError(approval_id=42, risk_level="high")
        )

        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            write_classifier=wc,
            approval_queue=approval_queue,
        )

        identity = _identity()
        sql_payload = b"DROP TABLE users\x00"

        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload,
            client_writer,
            upstream_reader,
            upstream_writer,
            identity=identity,
        )

        # Upstream should have received nothing
        assert len(upstream_buf) == 0

        response = bytes(client_buf)
        assert chr(response[0]) == "E"
        assert b"queued for approval" in response
        assert b"42" in response

        # Verify submit was called
        approval_queue.submit.assert_called_once()
        call_kwargs = approval_queue.submit.call_args
        assert call_kwargs[1]["identity_id"] == 1
        assert call_kwargs[1]["risk_level"] == RiskLevel.HIGH


# =========================================================================
# Extended protocol write safety
# =========================================================================


class TestExtendedProtocolWriteSafety:
    """Prepared writes must hit the same governance spine as SimpleQuery."""

    @pytest.mark.asyncio
    async def test_parse_drop_table_queued_for_authenticated_identity(self) -> None:
        wc = WriteClassifier()
        approval_queue = AsyncMock()
        approval_queue.submit = AsyncMock(
            side_effect=WriteApprovalRequiredError(
                approval_id=77,
                risk_level="high",
            )
        )
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            write_classifier=wc,
            approval_queue=approval_queue,
        )
        client_reader = _make_reader(
            _pack_parse("danger", "DROP TABLE users") + pack_message(MSG_TYPE_TERMINATE, b"")
        )
        client_writer, client_buf = _make_writer()
        upstream_reader = _make_reader(b"")
        upstream_writer, upstream_buf = _make_writer()

        await proxy._main_loop(
            client_reader,
            client_writer,
            upstream_reader,
            upstream_writer,
            identity=_identity(),
            source_id="customer-db",
        )

        assert bytes(upstream_buf) == pack_message(MSG_TYPE_TERMINATE, b"")
        assert b"queued for approval" in bytes(client_buf)
        approval_queue.submit.assert_awaited_once()
        assert approval_queue.submit.call_args.kwargs["source_id"] == "customer-db"

    @pytest.mark.asyncio
    async def test_parse_write_blocked_when_identity_unresolved(self) -> None:
        wc = WriteClassifier()
        proxy = PGProxy(listen_port=0, upstream_port=0, write_classifier=wc)
        client_writer, client_buf = _make_writer()

        decision = await proxy._guard_extended_sql(
            "DELETE FROM users",
            client_writer,
            identity=None,
            source_id="customer-db",
        )

        assert decision.allowed is False
        assert b"identity/source unresolved" in bytes(client_buf)

    @pytest.mark.asyncio
    async def test_execute_response_is_redacted_and_audited(self) -> None:
        audit = _AuditCollector()
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            pii_scanner=PIIFastScanner(),
            audit_logger=audit,
        )
        client_reader = _make_reader(
            _pack_parse("read_user", "SELECT ssn FROM users WHERE id = $1")
            + _pack_bind("", "read_user")
            + _pack_execute("")
            + _pack_sync()
            + pack_message(MSG_TYPE_TERMINATE, b"")
        )
        upstream_reader = _make_reader(
            pack_message("1", b"")
            + pack_message("2", b"")
            + _pack_data_row("123-45-6789")
            + _pack_command_complete("SELECT 1")
            + _pack_ready_for_query()
        )
        upstream_writer, _ = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._main_loop(
            client_reader,
            client_writer,
            upstream_reader,
            upstream_writer,
            identity=_identity(),
            source_id="customer-db",
        )

        response = bytes(client_buf)
        assert b"123-45-6789" not in response
        assert b"[REDACTED:SSN]" in response
        assert len(audit.records) == 1
        assert audit.records[0].operation == "read"
        assert audit.records[0].pii_detected is True
        assert "SSN" in audit.records[0].pii_types

    @pytest.mark.asyncio
    async def test_execute_write_invalidates_l1_and_audits_write(self) -> None:
        l1 = L1Cache(max_size=10, ttl_seconds=60)
        await l1.put("stale-read", b"cached")
        audit = _AuditCollector()
        proxy = PGProxy(
            listen_port=0,
            upstream_port=0,
            l1_cache=l1,
            audit_logger=audit,
            write_classifier=WriteClassifier(),
        )
        client_reader = _make_reader(
            _pack_parse("insert_log", "INSERT INTO logs (msg) VALUES ($1)")
            + _pack_bind("", "insert_log")
            + _pack_execute("")
            + _pack_sync()
            + pack_message(MSG_TYPE_TERMINATE, b"")
        )
        upstream_reader = _make_reader(
            pack_message("1", b"")
            + pack_message("2", b"")
            + _pack_command_complete("INSERT 0 1")
            + _pack_ready_for_query()
        )
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._main_loop(
            client_reader,
            client_writer,
            upstream_reader,
            upstream_writer,
            identity=_identity(),
            source_id="customer-db",
        )

        assert l1.stats["size"] == 0
        assert b"INSERT 0 1" in bytes(client_buf)
        assert len(upstream_buf) > 0
        assert len(audit.records) == 1
        assert audit.records[0].operation == "write"
        assert audit.records[0].source_id == "customer-db"


# =========================================================================
# Low-risk write passes through
# =========================================================================


class TestWriteClassifierLowRisk:
    """Test that low-risk writes (INSERT) pass through to upstream."""

    @pytest.mark.asyncio
    async def test_insert_passes_through(self) -> None:
        wc = WriteClassifier()

        proxy = PGProxy(listen_port=0, upstream_port=0, write_classifier=wc)

        sql_payload = b"INSERT INTO logs (msg) VALUES ('hello')\x00"
        cmd = _pack_command_complete("INSERT 0 1")
        rfq = _pack_ready_for_query()
        upstream_reader = _make_reader(cmd + rfq)
        upstream_writer, upstream_buf = _make_writer()
        client_writer, client_buf = _make_writer()

        await proxy._handle_simple_query(
            sql_payload,
            client_writer,
            upstream_reader,
            upstream_writer,
        )

        # Upstream should have received the query (low risk passes through)
        assert len(upstream_buf) > 0
        assert chr(upstream_buf[0]) == MSG_TYPE_QUERY

        # Client should have received the response
        assert bytes(client_buf) == cmd + rfq
