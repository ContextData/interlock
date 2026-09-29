"""PG Wire Protocol Proxy.

A minimal transparent TCP proxy that sits between PostgreSQL clients and
an upstream PostgreSQL server.  Most bytes pass through untouched; only
specific message types (SimpleQuery, Parse) are inspected so we can
normalise SQL, check the cache, and emit audit records.

Usage::

    proxy = PGProxy(listen_port=5432, upstream_port=5433)
    await proxy.start()
    # ... later ...
    await proxy.stop()
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import ssl
import struct
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from interlock.connections.source_config import (
    resolve_config_value,
    upstream_tls_refusal,
    validate_source_config,
)
from interlock.core.normalizer import compute_cache_key, normalize_sql
from interlock.errors import IdentityNotFoundError
from interlock.gateway.pg_messages import (
    CANCEL_REQUEST_CODE,
    DEFAULT_MAX_MESSAGE_PAYLOAD_BYTES,
    DEFAULT_MAX_STARTUP_PAYLOAD_BYTES,
    GSSENC_REQUEST_CODE,
    MSG_TYPE_COMMAND_COMPLETE,
    MSG_TYPE_ERROR_RESPONSE,
    MSG_TYPE_PARSE,
    MSG_TYPE_QUERY,
    MSG_TYPE_READY_FOR_QUERY,
    MSG_TYPE_TERMINATE,
    PROTOCOL_VERSION_3_0,
    SSL_REQUEST_CODE,
    PGFrameTooLargeError,
    PGProtocolError,
    extract_sql_from_query,
    pack_message,
    read_message,
    read_startup_message,
    write_message,
)
from interlock.gateway.pipeline import GatewayDecision, GatewayPipeline, GatewayRequest
from interlock.models import IdentityContext, NormalizedQuery

if TYPE_CHECKING:
    from interlock.audit.logger import AuditLogger
    from interlock.cache.invalidation import CacheInvalidator
    from interlock.cache.l1 import L1Cache
    from interlock.cache.l2 import L2Cache
    from interlock.cache.strategy import CacheStrategy, CacheStrategyResolver
    from interlock.core.approval_queue import ApprovalQueue
    from interlock.core.auth import AuthManager
    from interlock.core.policy import PolicyEngine
    from interlock.core.rate_limiter import RateLimiter
    from interlock.core.write_classifier import WriteClassifier
    from interlock.metadata.registry import MetadataRegistry
    from interlock.pipeline.pii_fast import PIIFastScanner

logger = logging.getLogger(__name__)

AUTH_OK = 0
AUTH_CLEARTEXT = 3
AUTH_MD5 = 5
AUTH_SASL = 10
AUTH_SASL_CONTINUE = 11
AUTH_SASL_FINAL = 12

# Extended-protocol clients may wait for Parse/Bind/Describe acknowledgements
# before sending the next phase. A tiny local-only timeout can deadlock against
# real upstream databases where the acknowledgement arrives after one network
# round trip, so keep this long enough for managed databases while still bounded
# for pipelined clients that legally defer responses until Sync.
EXTENDED_PHASE_RESPONSE_TIMEOUT_SECONDS = 1.0
DEFAULT_MAX_RESULT_BYTES = 64 * 1024 * 1024
DEFAULT_STARTUP_TIMEOUT_SECONDS = 5.0
DEFAULT_AUTH_TIMEOUT_SECONDS = 10.0
DEFAULT_FRAME_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONNECTIONS = 1024
DEFAULT_CANCEL_CONNECTION_RESERVE = 16
COPY_FRONTEND_MESSAGE_TYPES = {"c", "d", "f"}


# Backend messages a simple-query response may contain: RowDescription,
# DataRow, CommandComplete, EmptyQueryResponse, ErrorResponse, NoticeResponse,
# ParameterStatus, NotificationResponse and the closing ReadyForQuery.
_SIMPLE_QUERY_RESPONSE_TYPES = frozenset(b"TDCIENSAZ")


def is_simple_query_response(data: bytes) -> bool:
    """True when `data` is a complete simple-query response in wire format.

    A cached entry is written straight to the client's socket, so anything
    else would desynchronise the client. The cache key already separates
    protocols; this is the second line, so a future key collision degrades to
    a cache miss instead of a broken connection.
    """
    offset = 0
    last_type = 0
    size = len(data)
    while offset < size:
        if size - offset < 5:
            return False
        msg_type = data[offset]
        length = int.from_bytes(data[offset + 1 : offset + 5], "big")
        if msg_type not in _SIMPLE_QUERY_RESPONSE_TYPES or length < 4:
            return False
        offset += 1 + length
        if offset > size:
            return False
        last_type = msg_type
    return size > 0 and last_type == ord("Z")


@dataclass
class ExtendedExecutionContext:
    """Governance metadata for one extended-protocol Execute."""

    sql: str
    source_id: str
    identity: IdentityContext | None
    operation: str
    fingerprint: str | None
    tables: list[str]
    started_at: float
    decision: GatewayDecision | None = None


@dataclass
class PGCancelTarget:
    """Opaque client cancellation key mapped to one live upstream session."""

    proxy_process_id: int
    proxy_secret_key: int
    upstream_host: str
    upstream_port: int
    connection_config: dict[str, object]
    source_id: str
    upstream_process_id: int | None = None
    upstream_secret_key: int | None = None


def _tables_across_statements(statements: list[Any]) -> list[str]:
    """Every table any statement in a multi-statement message references."""
    seen: dict[str, None] = {}
    for statement in statements:
        if isinstance(statement, NormalizedQuery):
            for table in statement.tables:
                seen.setdefault(table, None)
    return list(seen)


class PGProxy:
    """Transparent TCP proxy for the PostgreSQL wire protocol.

    The proxy intercepts SimpleQuery ('Q') messages for cache lookup and
    Parse ('P') messages for SQL logging/normalisation.  All other traffic
    is forwarded byte-for-byte to the upstream server.
    """

    def __init__(
        self,
        listen_host: str = "0.0.0.0",
        listen_port: int = 5432,
        upstream_host: str = "localhost",
        upstream_port: int = 5433,
        normalizer_source_id: str = "default",
        l1_cache: L1Cache | None = None,
        l2_cache: L2Cache | None = None,
        audit_logger: AuditLogger | None = None,
        auth_manager: AuthManager | None = None,
        policy_engine: PolicyEngine | None = None,
        write_classifier: WriteClassifier | None = None,
        approval_queue: ApprovalQueue | None = None,
        pii_scanner: PIIFastScanner | None = None,
        cache_strategy: CacheStrategy | None = None,
        cache_strategies: CacheStrategyResolver | None = None,
        rate_limiter: RateLimiter | None = None,
        registry: MetadataRegistry | None = None,
        embedding_engine: object | None = None,
        source_role_evaluator: object | None = None,
        sql_naming: object | None = None,
        cache_invalidator: CacheInvalidator | None = None,
        max_startup_bytes: int = DEFAULT_MAX_STARTUP_PAYLOAD_BYTES,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_PAYLOAD_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        auth_timeout_seconds: float = DEFAULT_AUTH_TIMEOUT_SECONDS,
        frame_timeout_seconds: float = DEFAULT_FRAME_TIMEOUT_SECONDS,
        max_connections: int | None = DEFAULT_MAX_CONNECTIONS,
        allow_insecure_upstream_tls: bool = True,
        cache_barrier_strict: bool = False,
        client_ssl_context: ssl.SSLContext | None = None,
        require_client_tls: bool = False,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.normalizer_source_id = normalizer_source_id

        self._l1 = l1_cache
        self._l2 = l2_cache
        self._audit = audit_logger
        self._auth = auth_manager
        self._policy = policy_engine
        self._write_classifier = write_classifier
        self._approval_queue = approval_queue
        self._pii_scanner = pii_scanner
        self._cache_strategy = cache_strategy
        # Optional per-source resolver. When absent the single
        # strategy above is used, which is the pre-#53 behaviour and
        # keeps proxies constructed in tests working unchanged.
        self._cache_strategies = cache_strategies
        self._rate_limiter = rate_limiter
        self._embedding_engine = embedding_engine
        self._source_role_evaluator = source_role_evaluator
        # Catalog naming: the PG wire resolves table names as MCP does.
        self._sql_naming = sql_naming
        self._cache_invalidator = cache_invalidator
        self._max_startup_bytes = max_startup_bytes
        self._max_message_bytes = max_message_bytes
        self._max_result_bytes = max_result_bytes
        self._startup_timeout_seconds = startup_timeout_seconds
        self._auth_timeout_seconds = auth_timeout_seconds
        self._frame_timeout_seconds = frame_timeout_seconds
        self._max_connections = max_connections
        self._allow_insecure_upstream_tls = allow_insecure_upstream_tls
        self._cache_barrier_strict = cache_barrier_strict
        self._client_ssl_context = client_ssl_context
        self._require_client_tls = require_client_tls
        # AUDIT-COVERS: P1-C. When a registry is provided, the proxy
        # routes incoming connections to the upstream described by the
        # registered data source whose ``source_id`` matches the
        # database name in the startup message. Unknown databases are
        # rejected with a typed FATAL error rather than silently routed
        # to the configured ``upstream_host``.
        self._registry = registry

        self._server: asyncio.AbstractServer | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._active_connections: int = 0
        self._cancel_targets: dict[tuple[int, int], PGCancelTarget] = {}
        self._shutting_down: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the TCP proxy server."""
        self._server = await asyncio.start_server(
            self._handle_client,
            self.listen_host,
            self.listen_port,
        )
        addrs = [str(s.getsockname()) for s in self._server.sockets]
        logger.info("PGProxy listening on %s", addrs)

    async def stop(self) -> None:
        """Stop accepting new connections and drain existing ones."""
        self._shutting_down = True

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Cancel outstanding client handlers and wait for them
        if self._active_tasks:
            for task in self._active_tasks:
                task.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._active_tasks.clear()

        logger.info("PGProxy stopped")

    @property
    def active_connections(self) -> int:
        return self._active_connections

    async def _read_client_message(
        self,
        reader: asyncio.StreamReader,
        *,
        timeout: float | None = None,
    ) -> tuple[str, bytes]:
        return await asyncio.wait_for(
            read_message(reader, max_payload_bytes=self._max_message_bytes),
            timeout=timeout or self._frame_timeout_seconds,
        )

    async def _read_upstream_message(
        self,
        reader: asyncio.StreamReader,
        *,
        timeout: float | None = None,
    ) -> tuple[str, bytes]:
        return await asyncio.wait_for(
            read_message(reader, max_payload_bytes=self._max_message_bytes),
            timeout=timeout or self._frame_timeout_seconds,
        )

    async def _read_startup_frame(
        self,
        reader: asyncio.StreamReader,
    ) -> tuple[int, bytes]:
        return await asyncio.wait_for(
            read_startup_message(reader, max_payload_bytes=self._max_startup_bytes),
            timeout=self._startup_timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Per-connection handling
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection.

        Wrapped in a Task so the server can accept the next connection
        immediately.
        """
        task = asyncio.current_task()
        over_regular_limit = (
            self._max_connections is not None
            and self._max_connections >= 0
            and self._active_connections >= self._max_connections
        )
        over_absolute_limit = (
            self._max_connections is not None
            and self._max_connections >= 0
            and self._active_connections
            >= self._max_connections + DEFAULT_CANCEL_CONNECTION_RESERVE
        )
        if over_absolute_limit:
            client_writer.write(
                _build_error_response(
                    "FATAL",
                    "53300",
                    "too many PostgreSQL proxy connections",
                )
            )
            await client_writer.drain()
            _safe_close(client_writer)
            return

        if task is not None:
            self._active_tasks.add(task)

        self._active_connections += 1
        peer = client_writer.get_extra_info("peername")
        logger.info("PGProxy: New connection from %s", peer)

        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        cancel_target: PGCancelTarget | None = None

        try:
            # --- Startup handshake ---
            startup_data = await self._handle_startup(client_reader, client_writer)
            if startup_data is None:
                # SSL/GSS negotiation rejection, a handled CancelRequest, or
                # a client disconnect ends this one-shot connection.
                return

            if over_regular_limit:
                client_writer.write(
                    _build_error_response(
                        "FATAL",
                        "53300",
                        "too many PostgreSQL proxy connections",
                    )
                )
                await client_writer.drain()
                return

            # Extract user (and database) from startup for identity and
            # registry-based routing.
            startup_params = _extract_startup_params(startup_data)
            startup_user = startup_params.get("user")
            startup_db = startup_params.get("database") or startup_user
            session_source_id = self.normalizer_source_id
            source_cfg: dict[str, object] = {}

            # P1-C: registry-backed origin selection.
            upstream_host, upstream_port = self.upstream_host, self.upstream_port
            if self._registry is not None and startup_db:
                source = self._registry.get(startup_db)
                if source is None:
                    error_bytes = _build_error_response(
                        "FATAL",
                        "3D000",
                        f"Unknown data source: {startup_db}",
                    )
                    client_writer.write(error_bytes)
                    await client_writer.drain()
                    return
                session_source_id = source.source_id
                source_cfg = getattr(source, "connection_config", None) or {}
                validate_source_config(
                    source_cfg,
                    connector_key=getattr(source, "connector_key", None) or "postgresql",
                    source_type=str(getattr(source, "source_type", "postgresql")),
                    source_id=session_source_id,
                )
                upstream_host, upstream_port = _resolve_upstream_host_port(
                    source_cfg,
                    upstream_host,
                    upstream_port,
                )

            # InterLock owns client authentication. The proxy accepts a
            # PostgreSQL password exchange, resolves that credential to an
            # InterLock identity, then opens the upstream with service
            # credentials from the source config / secret refs.
            identity: IdentityContext | None = None
            if self._auth is not None:
                try:
                    identity = await self._authenticate_pg_client(
                        client_reader,
                        client_writer,
                        startup_user or "",
                    )
                except IdentityNotFoundError as exc:
                    logger.warning("PG client authentication failed: %s", exc)
                    error_bytes = _build_error_response(
                        "FATAL", "28P01", "Invalid InterLock PostgreSQL credentials"
                    )
                    client_writer.write(error_bytes)
                    await client_writer.drain()
                    return

            # Open upstream connection. PostgreSQL TLS is negotiated with an
            # SSLRequest packet before the normal startup message, so the raw
            # wire proxy cannot rely on asyncio.open_connection(ssl=...).
            try:
                upstream_reader, upstream_writer = await _open_upstream_connection(
                    upstream_host,
                    upstream_port,
                    source_cfg,
                    allow_insecure_tls=self._allow_insecure_upstream_tls,
                )
            except UpstreamConnectionRefusedError as exc:
                # Still before AuthenticationOk, so a FATAL is a valid reply.
                # These escaped to the ConnectionError handler below, which
                # logged the reason and closed the socket: the client saw only
                # a lost connection.
                logger.warning(
                    "PGProxy upstream refused peer=%s source=%s reason=%s",
                    peer,
                    session_source_id,
                    exc,
                )
                await self._send_pg_fatal(client_writer, "08004", str(exc))
                return
            except OSError as exc:
                # The detail can name an upstream address, so it stays in the log.
                logger.warning(
                    "PGProxy upstream unreachable peer=%s source=%s reason=%s",
                    peer,
                    session_source_id,
                    exc,
                )
                await self._send_pg_fatal(
                    client_writer, "08006", "Upstream PostgreSQL connection failed"
                )
                return
            cancel_target = self._new_cancel_target(
                upstream_host=upstream_host,
                upstream_port=upstream_port,
                connection_config=source_cfg,
                source_id=session_source_id,
            )

            upstream_startup, upstream_user, upstream_password = _build_upstream_startup(
                startup_data,
                source_cfg,
                startup_user,
                startup_db,
            )
            upstream_writer.write(upstream_startup)
            await upstream_writer.drain()

            if self._auth is None:
                # Backward-compatible transparent mode for tests/dev setups that
                # intentionally wire no InterLock AuthManager.
                authenticated = await self._forward_until_ready(
                    client_reader,
                    client_writer,
                    upstream_reader,
                    upstream_writer,
                    cancel_target=cancel_target,
                )
            else:
                authenticated = await self._complete_upstream_auth(
                    client_writer,
                    upstream_reader,
                    upstream_writer,
                    upstream_user=upstream_user or "",
                    upstream_password=upstream_password,
                    cancel_target=cancel_target,
                )
            if not authenticated:
                return

            # --- Main message loop ---
            await self._main_loop(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
                identity=identity,
                source_id=session_source_id,
            )

        except ConnectionError as exc:
            logger.warning("PGProxy connection failed peer=%s reason=%s", peer, exc)
        except asyncio.IncompleteReadError:
            logger.debug("Connection closed (peer=%s)", peer)
        except asyncio.CancelledError:
            logger.debug("Connection cancelled (peer=%s)", peer)
        except Exception:
            logger.exception("Unhandled error for peer=%s", peer)
        finally:
            if cancel_target is not None:
                self._unregister_cancel_target(cancel_target)
            self._active_connections -= 1
            _safe_close(client_writer)
            if upstream_writer is not None:
                _safe_close(upstream_writer)
            if task is not None:
                self._active_tasks.discard(task)

    # ------------------------------------------------------------------
    # Startup / SSL negotiation
    # ------------------------------------------------------------------

    async def _handle_startup(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> bytes | None:
        """Read the client startup message, handling SSL negotiation.

        Returns the raw startup message bytes to forward to upstream,
        or ``None`` if the client disconnected.
        """
        try:
            code, raw = await self._read_startup_frame(client_reader)
        except TimeoutError:
            await self._send_pg_fatal(
                client_writer,
                "57014",
                "PostgreSQL startup timed out",
            )
            return None
        except PGProtocolError as exc:
            await self._send_pg_fatal(client_writer, "54000", _protocol_error_message(exc))
            return None

        if code == SSL_REQUEST_CODE:
            if self._client_ssl_context is None:
                client_writer.write(b"N")
                await client_writer.drain()
                if self._require_client_tls:
                    return None
            else:
                client_writer.write(b"S")
                await client_writer.drain()
                await client_writer.start_tls(self._client_ssl_context)
            try:
                code, raw = await self._read_startup_frame(client_reader)
            except TimeoutError:
                await self._send_pg_fatal(
                    client_writer,
                    "57014",
                    "PostgreSQL startup timed out",
                )
                return None
            except PGProtocolError as exc:
                await self._send_pg_fatal(client_writer, "54000", _protocol_error_message(exc))
                return None

        elif self._require_client_tls:
            await self._send_pg_fatal(
                client_writer,
                "28000",
                "PostgreSQL client TLS is required",
            )
            return None

        if code == GSSENC_REQUEST_CODE:
            client_writer.write(b"N")
            await client_writer.drain()
            return None

        if code == CANCEL_REQUEST_CODE:
            await self._route_cancel_request(raw)
            return None

        if code != PROTOCOL_VERSION_3_0:
            await self._send_pg_fatal(
                client_writer,
                "0A000",
                "Unsupported PostgreSQL startup protocol",
            )
            return None

        return raw

    def _new_cancel_target(
        self,
        *,
        upstream_host: str,
        upstream_port: int,
        connection_config: dict[str, object],
        source_id: str,
    ) -> PGCancelTarget:
        """Create a collision-resistant client key for one live session."""
        while True:
            process_id = secrets.randbits(31) or 1
            secret_key = secrets.randbits(31) or 1
            if (process_id, secret_key) not in self._cancel_targets:
                return PGCancelTarget(
                    proxy_process_id=process_id,
                    proxy_secret_key=secret_key,
                    upstream_host=upstream_host,
                    upstream_port=upstream_port,
                    connection_config=dict(connection_config),
                    source_id=source_id,
                )

    def _register_backend_key(
        self,
        target: PGCancelTarget,
        upstream_payload: bytes,
    ) -> bytes:
        """Replace upstream BackendKeyData with an opaque proxy-owned key."""
        if len(upstream_payload) != 8:
            raise PGProtocolError("Invalid upstream BackendKeyData payload")

        upstream_process_id, upstream_secret_key = struct.unpack("!II", upstream_payload)
        target.upstream_process_id = upstream_process_id
        target.upstream_secret_key = upstream_secret_key
        self._cancel_targets[(target.proxy_process_id, target.proxy_secret_key)] = target
        return struct.pack("!II", target.proxy_process_id, target.proxy_secret_key)

    def _unregister_cancel_target(self, target: PGCancelTarget) -> None:
        key = (target.proxy_process_id, target.proxy_secret_key)
        if self._cancel_targets.get(key) is target:
            self._cancel_targets.pop(key, None)

    async def _route_cancel_request(self, raw: bytes) -> None:
        """Route a valid client cancellation key without exposing an oracle."""
        if len(raw) != 16:
            logger.warning("Rejected malformed PostgreSQL CancelRequest")
            return
        length, code, process_id, secret_key = struct.unpack("!IIII", raw)
        if length != 16 or code != CANCEL_REQUEST_CODE:
            logger.warning("Rejected malformed PostgreSQL CancelRequest")
            return

        target = self._cancel_targets.get((process_id, secret_key))
        if target is None:
            logger.info("Ignored PostgreSQL CancelRequest for an unknown or stale session")
            return

        try:
            await asyncio.wait_for(
                self._send_upstream_cancel(target),
                timeout=self._startup_timeout_seconds,
            )
        except (ConnectionError, OSError, TimeoutError):
            logger.warning(
                "Failed to route PostgreSQL CancelRequest source_id=%s",
                target.source_id,
            )
            return

        logger.info(
            "Routed PostgreSQL CancelRequest source_id=%s",
            target.source_id,
        )

    async def _send_upstream_cancel(self, target: PGCancelTarget) -> None:
        """Send PostgreSQL's one-shot CancelRequest to the original upstream."""
        if target.upstream_process_id is None or target.upstream_secret_key is None:
            raise ConnectionError("Upstream PostgreSQL cancellation key is unavailable")

        _reader, writer = await _open_upstream_connection(
            target.upstream_host,
            target.upstream_port,
            target.connection_config,
            allow_insecure_tls=self._allow_insecure_upstream_tls,
        )
        try:
            writer.write(
                struct.pack(
                    "!IIII",
                    16,
                    CANCEL_REQUEST_CODE,
                    target.upstream_process_id,
                    target.upstream_secret_key,
                )
            )
            await writer.drain()
        finally:
            _safe_close(writer)

    async def _authenticate_pg_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        startup_user: str,
    ) -> IdentityContext:
        """Run InterLock-owned PG password auth and return the identity."""
        # Keep the client-facing auth mechanism deliberately simple. We ask
        # for a PostgreSQL password and interpret it as either an InterLock
        # API key or an identity-scoped PG password.
        write_message(client_writer, "R", struct.pack(">I", AUTH_CLEARTEXT))
        await client_writer.drain()

        try:
            msg_type, payload = await self._read_client_message(
                client_reader,
                timeout=self._auth_timeout_seconds,
            )
        except TimeoutError as exc:
            raise IdentityNotFoundError("PostgreSQL password response timed out") from exc
        except PGProtocolError as exc:
            raise IdentityNotFoundError(_protocol_error_message(exc)) from exc
        if msg_type != "p":
            raise IdentityNotFoundError("Expected PostgreSQL password response")
        password = payload.rstrip(b"\x00").decode("utf-8", errors="replace")

        if hasattr(self._auth, "authenticate_pg"):
            return await self._auth.authenticate_pg(startup_user, password)  # type: ignore[union-attr]
        return await self._auth.authenticate(password)  # type: ignore[union-attr]

    async def _complete_upstream_auth(
        self,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        *,
        upstream_user: str,
        upstream_password: str | None,
        cancel_target: PGCancelTarget | None = None,
    ) -> bool:
        """Authenticate to upstream using service credentials.

        Upstream authentication messages are consumed by the proxy rather than
        forwarded to the client. Once upstream reaches ReadyForQuery, the
        client has received AuthenticationOk plus normal startup metadata.
        """
        scram_state: dict[str, str] | None = None

        while True:
            msg_type, payload = await self._read_upstream_message(upstream_reader)

            if msg_type == MSG_TYPE_ERROR_RESPONSE:
                write_message(client_writer, msg_type, payload)
                await client_writer.drain()
                logger.warning("Upstream service auth failed")
                return False

            if msg_type == "R" and len(payload) >= 4:
                auth_type = struct.unpack(">I", payload[:4])[0]
                logger.info("PGProxy upstream auth: type=%d", auth_type)

                if auth_type == AUTH_OK:
                    write_message(client_writer, msg_type, payload)
                    await client_writer.drain()
                    continue

                if auth_type == AUTH_CLEARTEXT:
                    if upstream_password is None:
                        await self._send_pg_fatal(
                            client_writer,
                            "08004",
                            "Upstream password is not configured for this source",
                        )
                        return False
                    write_message(
                        upstream_writer,
                        "p",
                        upstream_password.encode("utf-8") + b"\x00",
                    )
                    await upstream_writer.drain()
                    continue

                if auth_type == AUTH_MD5:
                    if upstream_password is None:
                        await self._send_pg_fatal(
                            client_writer,
                            "08004",
                            "Upstream password is not configured for this source",
                        )
                        return False
                    salt = payload[4:8]
                    md5_password = _pg_md5_password(
                        upstream_password,
                        upstream_user,
                        salt,
                    )
                    write_message(upstream_writer, "p", md5_password + b"\x00")
                    await upstream_writer.drain()
                    continue

                if auth_type == AUTH_SASL:
                    if upstream_password is None:
                        await self._send_pg_fatal(
                            client_writer,
                            "08004",
                            "Upstream password is not configured for this source",
                        )
                        return False
                    mechanisms = _parse_sasl_mechanisms(payload[4:])
                    if "SCRAM-SHA-256" not in mechanisms:
                        await self._send_pg_fatal(
                            client_writer,
                            "08004",
                            "Upstream only offered unsupported SASL mechanisms",
                        )
                        return False
                    scram_state, initial_payload = _scram_initial_response(upstream_user)
                    write_message(upstream_writer, "p", initial_payload)
                    await upstream_writer.drain()
                    continue

                if auth_type == AUTH_SASL_CONTINUE:
                    if scram_state is None or upstream_password is None:
                        await self._send_pg_fatal(
                            client_writer,
                            "08004",
                            "Unexpected upstream SASL continuation",
                        )
                        return False
                    final_payload = _scram_final_response(
                        scram_state,
                        payload[4:].decode("utf-8", errors="replace"),
                        upstream_password,
                    )
                    write_message(upstream_writer, "p", final_payload)
                    await upstream_writer.drain()
                    continue

                if auth_type == AUTH_SASL_FINAL:
                    if scram_state is not None:
                        _scram_verify_server_final(
                            scram_state,
                            payload[4:].decode("utf-8", errors="replace"),
                        )
                    continue

                await self._send_pg_fatal(
                    client_writer,
                    "08004",
                    f"Unsupported upstream authentication type: {auth_type}",
                )
                return False

            if msg_type == "K" and cancel_target is not None:
                payload = self._register_backend_key(cancel_target, payload)
            write_message(client_writer, msg_type, payload)
            await client_writer.drain()

            if msg_type == MSG_TYPE_READY_FOR_QUERY:
                return True

    async def _send_pg_fatal(
        self,
        client_writer: asyncio.StreamWriter,
        code: str,
        message: str,
    ) -> None:
        client_writer.write(_build_error_response("FATAL", code, message))
        await client_writer.drain()

    def _source_withdrawn(self, source_id: str | None) -> bool:
        """True when a registry-routed session's source is no longer served."""
        return (
            self._registry is not None
            and bool(source_id)
            and (self._registry.get(str(source_id)) is None)
        )

    async def _send_pg_error_ready(
        self,
        client_writer: asyncio.StreamWriter,
        code: str,
        message: str,
    ) -> None:
        client_writer.write(
            _build_error_response("ERROR", code, message)
            + pack_message(MSG_TYPE_READY_FOR_QUERY, b"I")
        )
        await client_writer.drain()

    # ------------------------------------------------------------------
    # Auth passthrough
    # ------------------------------------------------------------------

    async def _forward_until_ready(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        *,
        cancel_target: PGCancelTarget | None = None,
    ) -> bool:
        """Transparently relay the full auth exchange between client and
        upstream until ReadyForQuery.

        SCRAM-SHA-256 is a multi-round protocol:
          Server -> AuthenticationSASL (mechanism list)
          Client -> SASLInitialResponse (p message)
          Server -> AuthenticationSASLContinue
          Client -> SASLResponse (p message)
          Server -> AuthenticationSASLFinal
          Server -> AuthenticationOk
          Server -> ParameterStatus* / BackendKeyData / ReadyForQuery

        We forward every server message to the client and, when the server
        expects a client reply (auth sub-types 10, 11), we read the client's
        response and forward it to the server.
        """
        # Auth 'R' sub-types that require a client response
        auth_sasl = 10  # AuthenticationSASL
        auth_sasl_continue = 11  # AuthenticationSASLContinue
        auth_cleartext = 3  # AuthenticationCleartextPassword
        auth_md5 = 5  # AuthenticationMD5Password

        while True:
            msg_type, payload = await self._read_upstream_message(upstream_reader)
            if msg_type == "K" and cancel_target is not None:
                payload = self._register_backend_key(cancel_target, payload)
            write_message(client_writer, msg_type, payload)
            await client_writer.drain()

            if msg_type == MSG_TYPE_READY_FOR_QUERY:
                return True
            if msg_type == MSG_TYPE_ERROR_RESPONSE:
                logger.warning("Upstream auth error forwarded to client")
                return False

            # If this is an auth message that expects a client reply,
            # read the client's response and forward it upstream.
            if msg_type == "R" and len(payload) >= 4:
                import struct

                auth_type = struct.unpack(">I", payload[:4])[0]
                logger.info("PGProxy auth: type=%d", auth_type)
                if auth_type in (
                    auth_sasl,
                    auth_sasl_continue,
                    auth_cleartext,
                    auth_md5,
                ):
                    # Client will send a 'p' (password/SASL) message
                    logger.info("PGProxy: waiting for client auth response...")
                    client_msg_type, client_payload = await self._read_client_message(
                        client_reader,
                        timeout=self._auth_timeout_seconds,
                    )
                    logger.info(
                        "PGProxy: got client auth msg type=%r len=%d",
                        client_msg_type,
                        len(client_payload),
                    )
                    write_message(upstream_writer, client_msg_type, client_payload)
                    await upstream_writer.drain()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        identity: IdentityContext | None = None,
        source_id: str | None = None,
    ) -> None:
        """Single-threaded message relay with SQL interception.

        Reads each client message, forwards to upstream, and when the
        protocol requires a response (SimpleQuery 'Q' or Sync 'S'),
        reads the complete response from upstream and forwards it to
        the client.

        This sequential approach avoids concurrent reads on the same
        stream and correctly handles both simple and extended query
        protocols.
        """
        prepared_statements: dict[str, str] = {}
        prepared_statement_decisions: dict[str, GatewayDecision] = {}
        blocked_prepared_statements: set[str] = set()
        portals: dict[str, str] = {}
        blocked_portals: set[str] = set()
        pending_executions: list[ExtendedExecutionContext] = []
        pending_phase_responses: list[str] = []

        while True:
            try:
                msg_type, payload = await self._read_client_message(client_reader)
            except asyncio.IncompleteReadError:
                return
            except TimeoutError:
                await self._send_pg_error_ready(
                    client_writer,
                    "57014",
                    "PostgreSQL client frame timed out",
                )
                return
            except PGProtocolError as exc:
                await self._send_pg_error_ready(
                    client_writer,
                    "54000",
                    _protocol_error_message(exc),
                )
                return

            if msg_type == MSG_TYPE_TERMINATE:
                write_message(upstream_writer, msg_type, payload)
                await upstream_writer.drain()
                return

            if msg_type in COPY_FRONTEND_MESSAGE_TYPES:
                await self._send_pg_error_ready(
                    client_writer,
                    "0A000",
                    "COPY is not supported by the PostgreSQL proxy",
                )
                return

            if self._source_withdrawn(source_id):
                # The source was disabled or deleted after this session
                # connected. End the session with the connect-time refusal
                # rather than keep serving it, from cache or upstream. Every
                # message is checked, not only Query and Parse: a client
                # re-running a prepared statement sends just Bind and Execute.
                # An ERROR followed by ReadyForQuery, not a FATAL: a client
                # mid-pipeline reports a FATAL as a dropped connection and the
                # reason is lost. The session ends straight after either way.
                await self._send_pg_error_ready(
                    client_writer,
                    "3D000",
                    f"Unknown data source: {source_id}",
                )
                return

            if msg_type == MSG_TYPE_QUERY:
                # SimpleQuery protocol: Q -> (response until Z)
                await self._handle_simple_query(
                    payload,
                    client_writer,
                    upstream_reader,
                    upstream_writer,
                    identity=identity,
                    source_id=source_id or self.normalizer_source_id,
                )
            else:
                if msg_type == MSG_TYPE_PARSE:
                    statement_name, sql = _parse_statement_payload(payload)
                    if sql:
                        decision = await self._guard_extended_sql(
                            sql,
                            client_writer,
                            identity=identity,
                            source_id=source_id or self.normalizer_source_id,
                        )
                        if not decision.allowed:
                            prepared_statements.pop(statement_name, None)
                            prepared_statement_decisions.pop(statement_name, None)
                            blocked_prepared_statements.add(statement_name)
                            continue
                        blocked_prepared_statements.discard(statement_name)
                        prepared_statements[statement_name] = sql
                        prepared_statement_decisions[statement_name] = decision

                if msg_type == "C":
                    _apply_close_payload(
                        payload,
                        prepared_statements,
                        prepared_statement_decisions,
                        blocked_prepared_statements,
                        portals,
                        blocked_portals,
                    )

                if msg_type == "B":
                    portal_name, statement_name = _parse_bind_payload(payload)
                    if statement_name in blocked_prepared_statements:
                        blocked_portals.add(portal_name)
                        portals.pop(portal_name, None)
                        await self._reject_or_queue_extended_write(
                            client_writer,
                            "Rejected prepared statement cannot be bound safely",
                        )
                        continue
                    blocked_portals.discard(portal_name)
                    portals[portal_name] = statement_name

                if msg_type == "E":
                    portal_name = _parse_execute_payload(payload)
                    if portal_name in blocked_portals:
                        await self._reject_or_queue_extended_write(
                            client_writer,
                            "Rejected prepared statement cannot be executed safely",
                        )
                        continue
                    statement_name = portals.get(portal_name, portal_name)
                    sql = prepared_statements.get(statement_name)
                    if sql is None:
                        await self._reject_or_queue_extended_write(
                            client_writer,
                            "Unresolved prepared statement cannot be executed safely",
                        )
                        continue
                    pending_executions.append(
                        self._build_extended_execution_context(
                            sql,
                            identity=identity,
                            source_id=source_id or self.normalizer_source_id,
                            decision=prepared_statement_decisions.get(statement_name),
                        )
                    )

                # Extended query protocol and all other messages:
                # Forward to upstream.
                write_message(upstream_writer, msg_type, payload)
                await upstream_writer.drain()

                if msg_type in (MSG_TYPE_PARSE, "B", "C"):
                    pending_phase_responses.append("one")
                elif msg_type == "D":
                    pending_phase_responses.append(
                        "describe_statement" if payload[:1] == b"S" else "one"
                    )

                # After forwarding, drain any available upstream responses
                # back to the client. This handles both pipelined and
                # non-pipelined extended query protocol:
                # - Non-pipelined: Parse -> ParseComplete comes immediately
                # - Pipelined: responses buffered until Sync
                # - Sync: responses end with ReadyForQuery
                if msg_type == "S":
                    await self._handle_extended_sync_response(
                        upstream_reader,
                        client_writer,
                        pending_executions,
                    )
                    pending_executions = []
                    pending_phase_responses = []
                elif msg_type == "E":
                    # Hold Execute responses until Sync so rows cannot slip
                    # past redaction/audit just because bytes are already
                    # available on the upstream socket.
                    continue
                elif msg_type in (MSG_TYPE_PARSE, "B", "C", "D"):
                    # PostgreSQL may buffer these acknowledgements until the
                    # client sends Flush or Sync. Waiting here deadlocks legal
                    # pipelined clients because their Flush is still sitting
                    # in the client stream. The exact expected response shape
                    # is tracked and drained at that protocol boundary.
                    continue
                elif msg_type == "H":
                    # Flush is the extended-protocol client's explicit request
                    # for outstanding Parse/Bind/Describe responses. Managed
                    # databases can take longer than a localhost round trip to
                    # return those bytes, so use the bounded phase timeout here
                    # instead of the tiny opportunistic drain used elsewhere.
                    await self._handle_extended_flush_response(
                        upstream_reader,
                        client_writer,
                        pending_executions,
                        pending_phase_responses,
                    )
                    pending_executions = []
                    pending_phase_responses = []
                else:
                    await self._drain_available(upstream_reader, client_writer)

    def _build_extended_execution_context(
        self,
        sql: str,
        *,
        identity: IdentityContext | None,
        source_id: str,
        decision: GatewayDecision | None = None,
    ) -> ExtendedExecutionContext:
        role_context = _role_context(identity)
        normalized = normalize_sql(sql, source_id, role_context=role_context)
        if isinstance(normalized, list):
            normalized = normalized[0] if normalized else None

        fingerprint: str | None = None
        tables: list[str] = []
        operation: str | None = None
        if isinstance(normalized, NormalizedQuery):
            fingerprint = compute_cache_key(
                protocol="postgresql",
                source_id=source_id,
                normalized_sql=normalized.normalized_sql or "",
                parameters=normalized.parameters,
                identity_role=role_context,
                mapped_pg_role=identity.mapped_pg_role if identity else None,
                tenant_id=getattr(identity, "team", None) if identity else None,
                grants_version=_grants_version(identity),
                policy_scope_hash=_decision_scope_hash(decision),
                source_generation=(
                    decision.source_generation
                    if decision is not None and decision.source_generation is not None
                    else None
                ),
            )
            tables = normalized.tables
            operation = normalized.operation

        op_type = _classify_operation(operation, sql)
        if decision is not None and decision.write_classification is not None:
            op_type = "write"

        return ExtendedExecutionContext(
            sql=sql,
            source_id=source_id,
            identity=identity,
            operation=op_type,
            fingerprint=fingerprint,
            tables=tables,
            started_at=time.monotonic(),
            decision=decision,
        )

    async def _handle_extended_sync_response(
        self,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        executions: list[ExtendedExecutionContext],
    ) -> None:
        """Collect, redact, audit, and forward an extended response batch."""
        try:
            upstream_bytes = await self._collect_silent(upstream_reader)
        except TimeoutError as exc:
            await self._send_pg_error_ready(
                client_writer,
                "57014",
                "PostgreSQL upstream frame timed out",
            )
            raise ConnectionError("upstream PostgreSQL frame timed out") from exc
        except PGProtocolError as exc:
            await self._send_pg_error_ready(client_writer, "54000", _protocol_error_message(exc))
            return
        if self._pii_scanner is not None and upstream_bytes:
            response_bytes, pii_detected, pii_types = self._redact_response_bytes(upstream_bytes)
        else:
            response_bytes = upstream_bytes
            pii_detected = False
            pii_types = []

        for execution in executions:
            latency_ms = (time.monotonic() - execution.started_at) * 1000
            await self._emit_audit(
                sql=execution.sql,
                fingerprint=execution.fingerprint,
                cache_hit=False,
                cache_tier=None,
                latency_ms=latency_ms,
                pii_detected=pii_detected,
                pii_types=pii_types,
                source_id=execution.source_id,
                identity=execution.identity,
                operation=execution.operation,
                decision=execution.decision,
            )

        write_executions = [e for e in executions if e.operation == "write"]
        if write_executions:
            tables = sorted({table for e in write_executions for table in e.tables})
            await self._invalidate_caches_after_write(
                write_executions[0].source_id,
                tables,
                source_generation=_max_source_generation(write_executions),
            )

        if response_bytes:
            client_writer.write(response_bytes)
            await client_writer.drain()

    async def _handle_extended_flush_response(
        self,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        executions: list[ExtendedExecutionContext],
        phase_responses: list[str],
    ) -> None:
        """Collect, redact, audit, and forward a bounded Flush response batch."""
        try:
            upstream_bytes = await self._collect_flush_silent(
                upstream_reader,
                phase_responses=phase_responses,
                execution_count=len(executions),
            )
        except PGProtocolError as exc:
            await self._send_pg_error_ready(client_writer, "54000", _protocol_error_message(exc))
            return
        if self._pii_scanner is not None and upstream_bytes:
            response_bytes, pii_detected, pii_types = self._redact_response_bytes(upstream_bytes)
        else:
            response_bytes = upstream_bytes
            pii_detected = False
            pii_types = []

        for execution in executions:
            latency_ms = (time.monotonic() - execution.started_at) * 1000
            await self._emit_audit(
                sql=execution.sql,
                fingerprint=execution.fingerprint,
                cache_hit=False,
                cache_tier=None,
                latency_ms=latency_ms,
                pii_detected=pii_detected,
                pii_types=pii_types,
                source_id=execution.source_id,
                identity=execution.identity,
                operation=execution.operation,
                decision=execution.decision,
            )

        write_executions = [e for e in executions if e.operation == "write"]
        if write_executions:
            tables = sorted({table for e in write_executions for table in e.tables})
            await self._invalidate_caches_after_write(
                write_executions[0].source_id,
                tables,
                source_generation=_max_source_generation(write_executions),
            )

        if response_bytes:
            client_writer.write(response_bytes)
            await client_writer.drain()

    async def _guard_extended_sql(
        self,
        sql: str,
        client_writer: asyncio.StreamWriter,
        *,
        identity: IdentityContext | None,
        source_id: str,
    ) -> GatewayDecision:
        """Apply policy/write safety before forwarding extended Parse."""
        if _is_copy_sql(sql):
            reason = "COPY is not supported by the PostgreSQL proxy"
            await self._reject_or_queue_extended_write(client_writer, reason)
            return GatewayDecision(allowed=False, reason=reason, status_code=403)

        normalized = normalize_sql(sql, source_id, role_context=_role_context(identity))
        if isinstance(normalized, list):
            normalized = normalized[0] if normalized else None
        operation = normalized.operation if isinstance(normalized, NormalizedQuery) else None
        tables = normalized.tables if isinstance(normalized, NormalizedQuery) else []
        op_type = _classify_operation(operation, sql)

        gateway_request = GatewayRequest(
            protocol="postgresql",
            source_id=source_id,
            operation=op_type,
            identity=identity,
            normalized_request=normalized,
            sql=sql,
            tables=tables or None,
            metadata={"mode": "extended"},
        )
        decision = await self._pipeline().preflight(gateway_request)
        if not decision.allowed:
            await self._reject_or_queue_extended_write(
                client_writer,
                decision.reason,
                sqlstate="53400" if decision.status_code == 429 else "42501",
            )
            await self._pipeline().audit(
                gateway_request,
                decision=decision,
                status="queued" if decision.approval_state == "pending" else "denied",
                error_message=decision.reason,
            )
            return decision

        return decision

    async def _reject_or_queue_extended_write(
        self,
        client_writer: asyncio.StreamWriter,
        message: str,
        *,
        sqlstate: str = "0A000",
    ) -> None:
        error = _build_error_response("ERROR", sqlstate, message)
        rfq = pack_message(MSG_TYPE_READY_FOR_QUERY, b"I")
        client_writer.write(error + rfq)
        await client_writer.drain()

    # ------------------------------------------------------------------
    # Simple Query handler (cache-enabled)
    # ------------------------------------------------------------------

    async def _handle_simple_query(
        self,
        payload: bytes,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        identity: IdentityContext | None = None,
        source_id: str | None = None,
    ) -> None:
        """Handle a SimpleQuery ('Q') message with cache integration."""
        t0 = time.monotonic()
        sql = extract_sql_from_query(payload)
        source_id = source_id or self.normalizer_source_id
        if _is_copy_sql(sql):
            await self._send_pg_error_ready(
                client_writer,
                "0A000",
                "COPY is not supported by the PostgreSQL proxy",
            )
            return

        # P0-C: identity-scoped fingerprint. The role context is the
        # primary effective role (or mapped PG role when present) so a
        # cache entry generated for one identity cannot be served to a
        # caller with a different effective scope.
        role_context = None
        if identity is not None:
            role_context = identity.mapped_pg_role or (
                identity.roles[0] if identity.roles else None
            )

        # Normalise
        normalized = normalize_sql(sql, source_id, role_context=role_context)
        is_multi_statement = isinstance(normalized, list)
        statements = normalized if isinstance(normalized, list) else [normalized]
        # normalize_sql can return a list for multi-statement; use the first
        # statement only as request metadata. Multi-statement SQL is never
        # cacheable because a single-statement fingerprint cannot safely
        # represent the full response or session effects.
        if isinstance(normalized, list):
            normalized = normalized[0] if normalized else None

        fingerprint: str | None = None
        tables: list[str] = []
        operation: str | None = None
        cache_source_generation = await self._source_generation(source_id)
        if isinstance(normalized, NormalizedQuery) and not is_multi_statement:
            # Re-derive an extended cache key that includes the full
            # identity context. This is what gets used for cache I/O so
            # that role/tenant/policy-scope are part of the bucketing.
            from interlock.core.normalizer import compute_cache_key

            fingerprint = compute_cache_key(
                protocol="postgresql",
                source_id=source_id,
                normalized_sql=normalized.normalized_sql or "",
                parameters=normalized.parameters,
                identity_role=role_context,
                mapped_pg_role=identity.mapped_pg_role if identity else None,
                tenant_id=getattr(identity, "team", None) if identity else None,
                grants_version=_grants_version(identity),
                source_generation=cache_source_generation,
            )
            tables = normalized.tables
            operation = normalized.operation
        elif is_multi_statement:
            # Governance has to see every table any statement reads. Keeping only
            # the first statement's tables let `SELECT 1; SELECT * FROM orders`
            # through a deny on `orders`: the request carried no tables, so the
            # evaluator saw resource `*.*` and the policy `tables` condition had
            # nothing to intersect. Writes in a multi-statement message are
            # already refused by the write classifier before this point matters.
            tables = _tables_across_statements(statements)
            operation = normalized.operation if isinstance(normalized, NormalizedQuery) else None

        # --- Determine operation type for policy ---
        op_type = _classify_operation(operation, sql)

        # --- Shared gateway governance check ---
        gateway_request = GatewayRequest(
            protocol="postgresql",
            source_id=source_id,
            operation=op_type,
            identity=identity,
            normalized_request=normalized,
            sql=sql,
            tables=tables or None,
            metadata={"mode": "simple"},
        )
        decision = await self._pipeline().preflight(gateway_request)
        if not decision.allowed:
            logger.info("PG governance denied query: %s", decision.reason)
            sqlstate = "53400" if decision.status_code == 429 else "42501"
            error_bytes = _build_error_response("ERROR", sqlstate, decision.reason)
            rfq = pack_message(MSG_TYPE_READY_FOR_QUERY, b"I")
            client_writer.write(error_bytes + rfq)
            await client_writer.drain()
            await self._pipeline().audit(
                gateway_request,
                decision=decision,
                fingerprint=fingerprint,
                latency_ms=(time.monotonic() - t0) * 1000,
                status="queued" if decision.approval_state == "pending" else "denied",
                error_message=decision.reason,
            )
            return

        if decision.write_classification is not None:
            op_type = "write"
            operation = decision.write_classification.operation_type
            classified_tables = list(decision.write_classification.tables or [])
            if classified_tables:
                tables = sorted(set(tables + classified_tables))
        if decision.source_generation is not None:
            cache_source_generation = decision.source_generation

        if isinstance(normalized, NormalizedQuery) and not is_multi_statement:
            fingerprint = compute_cache_key(
                protocol="postgresql",
                source_id=source_id,
                normalized_sql=normalized.normalized_sql or "",
                parameters=normalized.parameters,
                identity_role=role_context,
                mapped_pg_role=identity.mapped_pg_role if identity else None,
                tenant_id=getattr(identity, "team", None) if identity else None,
                grants_version=_grants_version(identity),
                policy_scope_hash=_decision_scope_hash(decision),
                source_generation=cache_source_generation,
            )

        # --- Compute deterministic cache intent ---
        # Semantic SQL cache is deliberately disabled here until semantic
        # lookups include source, identity/team, effective grants, and
        # policy-scope filters. Deterministic L1/L2 remains available for
        # governed reads.
        intent_text: str | None = None
        intent_embedding: list[float] | None = None

        # --- Cache lookup ---
        cached_bytes: bytes | None = None
        cache_tier: str | None = None

        if op_type == "read" and fingerprint is not None:
            source_strategy = self._strategy_for(source_id)
            if source_strategy is not None:
                try:
                    result = await source_strategy.get(
                        fingerprint,
                        intent_text=intent_text,
                        intent_embedding=intent_embedding,
                    )
                    if result.hit and result.data is not None:
                        cached_bytes = result.data
                        cache_tier = result.tier
                except Exception:
                    logger.debug("Cache strategy get failed", exc_info=True)
            if cached_bytes is None:
                cached_bytes, cache_tier = await self._cache_get(fingerprint)
            if cached_bytes is not None and not is_simple_query_response(cached_bytes):
                logger.warning(
                    "Ignoring a cached entry that is not a PostgreSQL response source=%s",
                    source_id,
                )
                cached_bytes, cache_tier = None, None

        if cached_bytes is not None:
            latency_ms = (time.monotonic() - t0) * 1000
            await self._emit_audit(
                sql=sql,
                fingerprint=fingerprint,
                cache_hit=True,
                cache_tier=cache_tier,
                latency_ms=latency_ms,
                source_id=source_id,
                identity=identity,
                operation=op_type,
                decision=decision,
            )
            # Strict durability must accept the final audit event before any
            # successful cached response becomes visible to the client.
            client_writer.write(cached_bytes)
            await client_writer.drain()
            return

        # --- Cache miss: forward to upstream ---
        write_message(upstream_writer, MSG_TYPE_QUERY, payload)
        await upstream_writer.drain()

        # P0-D: collect upstream silently, redact DataRow fields, then
        # forward the redacted bytes to the client. Cache stores only
        # the post-redaction bytes so subsequent identities cannot pull
        # un-redacted data from cache.
        try:
            upstream_bytes = await self._collect_silent(upstream_reader)
        except TimeoutError as exc:
            await self._send_pg_error_ready(
                client_writer,
                "57014",
                "PostgreSQL upstream frame timed out",
            )
            raise ConnectionError("upstream PostgreSQL frame timed out") from exc
        except PGProtocolError as exc:
            await self._send_pg_error_ready(client_writer, "54000", _protocol_error_message(exc))
            return
        if self._pii_scanner is not None and upstream_bytes:
            response_bytes, pii_detected, pii_types = self._redact_response_bytes(upstream_bytes)
        else:
            response_bytes = upstream_bytes
            pii_detected = False
            pii_types = []

        latency_ms = (time.monotonic() - t0) * 1000
        await self._emit_audit(
            sql=sql,
            fingerprint=fingerprint,
            cache_hit=False,
            cache_tier=None,
            latency_ms=latency_ms,
            pii_detected=pii_detected,
            pii_types=pii_types,
            source_id=source_id,
            identity=identity,
            operation=op_type,
            decision=decision,
        )

        if op_type == "write":
            await self._invalidate_caches_after_write(
                source_id,
                tables,
                source_generation=decision.source_generation,
            )

        # Store only successful read responses after durable audit admission.
        if op_type == "read" and fingerprint is not None and response_bytes:
            source_strategy = self._strategy_for(source_id)
            if source_strategy is not None:
                try:
                    await source_strategy.put(
                        fingerprint,
                        response_bytes,
                        {"source_id": source_id, "protocol": "pg", "tables": tables},
                        intent_text=intent_text,
                        intent_embedding=intent_embedding,
                    )
                    if self._cache_invalidator is not None:
                        await self._cache_invalidator.record_dependency(
                            fingerprint,
                            source_id=source_id,
                            tables=tables,
                            protocol="pg",
                            metadata={"source_id": source_id, "protocol": "pg", "tables": tables},
                        )
                except Exception:
                    logger.debug("Cache strategy put failed", exc_info=True)
            else:
                await self._cache_put(
                    fingerprint,
                    response_bytes,
                    source_id=source_id,
                    tables=tables,
                )

        # Only expose the successful result after the strict audit contract and
        # write/cache consistency work have completed.
        if response_bytes:
            client_writer.write(response_bytes)
            await client_writer.drain()

    # ------------------------------------------------------------------
    # Response draining
    # ------------------------------------------------------------------

    async def _drain_available(
        self,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        *,
        timeout: float = 0.05,
    ) -> None:
        """Forward any immediately available upstream messages to the client.

        Uses a short timeout to avoid blocking: if the upstream has a
        response ready (e.g. ParseComplete after a standalone Parse), it
        will arrive within milliseconds.  If nothing arrives, we return
        so the main loop can read the next client message.
        """
        total_bytes = 0
        while True:
            if not _reader_has_buffered_data(upstream_reader):
                break
            try:
                # Peek to see if data is available; short timeout
                msg_type, payload = await self._read_upstream_message(
                    upstream_reader,
                    timeout=timeout,
                )
                raw = pack_message(msg_type, payload)
                total_bytes = self._checked_result_size(total_bytes, raw)
                client_writer.write(raw)
                await client_writer.drain()

                # If we got ReadyForQuery, stop draining
                if msg_type == MSG_TYPE_READY_FOR_QUERY:
                    break
            except TimeoutError as exc:
                raise ConnectionError("upstream PostgreSQL frame timed out") from exc

    async def _forward_one_upstream_message(
        self,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        *,
        phase: str,
    ) -> None:
        """Forward one immediate upstream response message to the client.

        Extended-query clients often pipeline Parse/Bind/Describe/Execute/Sync
        without a Flush between phases. In that case PostgreSQL may not emit
        the phase acknowledgement until Sync, so this helper must never block
        the client-to-upstream relay waiting for a response that is legally
        delayed.
        """
        try:
            msg_type, payload = await self._read_upstream_message(
                upstream_reader,
                timeout=EXTENDED_PHASE_RESPONSE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise ConnectionError(f"upstream PostgreSQL {phase} response timed out") from exc
        write_message(client_writer, msg_type, payload)
        await client_writer.drain()

    async def _forward_describe_response(
        self,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Forward the bounded response to an extended-protocol Describe.

        Statement Describe commonly returns ParameterDescription ('t') followed
        by RowDescription ('T') or NoData ('n'). Portal Describe usually returns
        just RowDescription/NoData. Keep this bounded so Execute DataRow bytes
        still wait for Sync redaction/audit.
        """
        try:
            msg_type, payload = await self._read_upstream_message(
                upstream_reader,
                timeout=EXTENDED_PHASE_RESPONSE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise ConnectionError("upstream PostgreSQL Describe response timed out") from exc
        write_message(client_writer, msg_type, payload)
        if msg_type == "t":
            try:
                msg_type, payload = await self._read_upstream_message(
                    upstream_reader,
                    timeout=EXTENDED_PHASE_RESPONSE_TIMEOUT_SECONDS,
                )
                write_message(client_writer, msg_type, payload)
            except TimeoutError as exc:
                raise ConnectionError(
                    "upstream PostgreSQL Describe row response timed out"
                ) from exc
        await client_writer.drain()

    # ------------------------------------------------------------------
    # Response collection
    # ------------------------------------------------------------------

    async def _collect_until_ready(
        self,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> bytes:
        """Read upstream messages until ReadyForQuery, forwarding each to
        the client.  Returns the concatenated raw bytes of the complete
        response (suitable for caching).

        DEPRECATED for the cache-miss path: use ``_collect_silent`` and
        ``_redact_response_bytes`` instead so PII is rewritten on the
        wire before the client sees it (P0-D).
        """
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            msg_type, payload = await self._read_upstream_message(upstream_reader)
            raw = pack_message(msg_type, payload)
            total_bytes = self._checked_result_size(total_bytes, raw)
            chunks.append(raw)
            client_writer.write(raw)
            await client_writer.drain()

            if msg_type == MSG_TYPE_READY_FOR_QUERY:
                break

        return b"".join(chunks)

    async def _collect_silent(
        self,
        upstream_reader: asyncio.StreamReader,
    ) -> bytes:
        """Collect upstream messages until ReadyForQuery without writing
        to the client. Used by the redaction path so we can rewrite
        DataRow fields before forwarding (P0-D)."""
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            msg_type, payload = await self._read_upstream_message(upstream_reader)
            raw = pack_message(msg_type, payload)
            total_bytes = self._checked_result_size(total_bytes, raw)
            chunks.append(raw)
            if msg_type == MSG_TYPE_READY_FOR_QUERY:
                break
        return b"".join(chunks)

    async def _collect_available_silent(
        self,
        upstream_reader: asyncio.StreamReader,
        *,
        timeout: float,
    ) -> bytes:
        """Collect immediately available upstream messages without forwarding."""
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            try:
                msg_type, payload = await self._read_upstream_message(
                    upstream_reader,
                    timeout=timeout,
                )
            except TimeoutError as exc:
                raise ConnectionError("upstream PostgreSQL frame timed out") from exc
            raw = pack_message(msg_type, payload)
            total_bytes = self._checked_result_size(total_bytes, raw)
            chunks.append(raw)
            if msg_type == MSG_TYPE_READY_FOR_QUERY:
                break
            timeout = 0.01
        return b"".join(chunks)

    async def _collect_flush_silent(
        self,
        upstream_reader: asyncio.StreamReader,
        *,
        phase_responses: list[str],
        execution_count: int,
    ) -> bytes:
        """Collect the exact response batch made visible by a Flush.

        Flush has no backend marker of its own. We therefore consume the
        acknowledgements implied by the already-forwarded frontend messages,
        followed by one terminal response per Execute. This avoids both the
        Parse/Flush deadlock and opportunistic reads that could split a frame.
        """
        chunks: list[bytes] = []
        total_bytes = 0

        async def receive() -> tuple[str, bytes]:
            nonlocal total_bytes
            msg_type, payload = await self._read_upstream_message(
                upstream_reader,
                timeout=EXTENDED_PHASE_RESPONSE_TIMEOUT_SECONDS,
            )
            raw = pack_message(msg_type, payload)
            total_bytes = self._checked_result_size(total_bytes, raw)
            chunks.append(raw)
            return msg_type, payload

        for expected in phase_responses:
            msg_type, _ = await receive()
            if msg_type == MSG_TYPE_ERROR_RESPONSE:
                return b"".join(chunks)
            if expected == "describe_statement" and msg_type == "t":
                msg_type, _ = await receive()
                if msg_type == MSG_TYPE_ERROR_RESPONSE:
                    return b"".join(chunks)

        for _ in range(execution_count):
            while True:
                msg_type, _ = await receive()
                if msg_type in {MSG_TYPE_COMMAND_COMPLETE, "s", MSG_TYPE_ERROR_RESPONSE}:
                    break

        return b"".join(chunks)

    def _checked_result_size(self, current_bytes: int, chunk: bytes) -> int:
        next_bytes = current_bytes + len(chunk)
        if self._max_result_bytes >= 0 and next_bytes > self._max_result_bytes:
            raise PGFrameTooLargeError(
                f"PG result payload length {next_bytes} exceeds limit {self._max_result_bytes}"
            )
        return next_bytes

    # ------------------------------------------------------------------
    # PII scanning
    # ------------------------------------------------------------------

    def _redact_response_bytes(self, response_bytes: bytes) -> tuple[bytes, bool, list[str]]:
        """Walk DataRow messages and rewrite PII fields in place.

        AUDIT-COVERS: P0-D

        For each DataRow ('D') message, every UTF-8-decodable text field
        is scanned with the PIIFastScanner. When matches are found the
        field's bytes are replaced with the redacted form (e.g.
        ``[REDACTED:SSN]``) and the message length / field length headers
        are rewritten so the wire format remains valid.

        Returns ``(rewritten_bytes, pii_detected, pii_types)``. If the
        scanner is not configured the original bytes are returned with
        ``pii_detected=False``.
        """
        if self._pii_scanner is None:
            return response_bytes, False, []

        out = bytearray()
        pii_types_set: set[str] = set()
        pii_detected = False

        offset = 0
        length = len(response_bytes)

        while offset < length:
            if offset + 5 > length:
                # Trailing junk - copy through.
                out.extend(response_bytes[offset:])
                break

            msg_type_byte = response_bytes[offset : offset + 1]
            msg_len = struct.unpack(">I", response_bytes[offset + 1 : offset + 5])[0]
            total_msg_len = 1 + msg_len

            if msg_type_byte != b"D" or offset + total_msg_len > length:
                out.extend(response_bytes[offset : offset + total_msg_len])
                offset += total_msg_len
                continue

            payload_start = offset + 5
            payload_end = offset + total_msg_len
            field_count = struct.unpack(">H", response_bytes[payload_start : payload_start + 2])[0]

            new_fields: list[bytes] = []
            pos = payload_start + 2
            ok = True
            for _ in range(field_count):
                if pos + 4 > payload_end:
                    ok = False
                    break
                field_len = struct.unpack(">i", response_bytes[pos : pos + 4])[0]
                pos += 4
                if field_len < 0:
                    new_fields.append(struct.pack(">i", -1))
                    continue
                if pos + field_len > payload_end:
                    ok = False
                    break

                raw = bytes(response_bytes[pos : pos + field_len])
                pos += field_len

                # Try to decode as UTF-8; if not text, pass through.
                try:
                    text_val = raw.decode("utf-8")
                except UnicodeDecodeError:
                    new_fields.append(struct.pack(">i", field_len) + raw)
                    continue

                matches = self._pii_scanner.scan(text_val)
                if not matches:
                    new_fields.append(struct.pack(">i", field_len) + raw)
                    continue

                pii_detected = True
                for m in matches:
                    pii_types_set.add(m.entity_type)
                redacted = self._pii_scanner.redact(text_val, matches).encode("utf-8")
                new_fields.append(struct.pack(">i", len(redacted)) + redacted)

            if not ok:
                # Defensive: forward the original message rather than
                # corrupt the wire stream on a parse error.
                out.extend(response_bytes[offset : offset + total_msg_len])
                offset += total_msg_len
                continue

            new_payload = struct.pack(">H", field_count) + b"".join(new_fields)
            new_msg_len = 4 + len(new_payload)  # length-self-inclusive
            out.append(ord("D"))
            out.extend(struct.pack(">I", new_msg_len))
            out.extend(new_payload)
            offset += total_msg_len

        return bytes(out), pii_detected, sorted(pii_types_set)

    def _scan_response_for_pii(self, response_bytes: bytes) -> tuple[bool, list[str]]:
        """Scan PG response bytes for PII (detection-only).

        Walks DataRow messages ('D'), extracts text fields, and scans
        each with PIIFastScanner. Returns (pii_detected, pii_types).
        """
        if self._pii_scanner is None:
            return False, []

        pii_types_set: set[str] = set()
        offset = 0
        length = len(response_bytes)

        while offset < length:
            if offset + 5 > length:
                break
            msg_type_byte = response_bytes[offset : offset + 1]
            msg_len = struct.unpack(">I", response_bytes[offset + 1 : offset + 5])[0]
            total_msg_len = 1 + msg_len  # type byte + length (includes itself)

            if msg_type_byte == b"D":
                # DataRow: 2-byte field count, then per field: 4-byte len + data
                payload_start = offset + 5
                payload_end = offset + total_msg_len
                if payload_end > length or payload_start + 2 > length:
                    offset += total_msg_len
                    continue

                field_count = struct.unpack(
                    ">H", response_bytes[payload_start : payload_start + 2]
                )[0]
                pos = payload_start + 2

                for _ in range(field_count):
                    if pos + 4 > payload_end:
                        break
                    field_len = struct.unpack(">i", response_bytes[pos : pos + 4])[0]
                    pos += 4
                    if field_len < 0:
                        # NULL field
                        continue
                    if pos + field_len > payload_end:
                        break
                    try:
                        text_val = response_bytes[pos : pos + field_len].decode(
                            "utf-8", errors="replace"
                        )
                        matches = self._pii_scanner.scan(text_val)
                        for m in matches:
                            pii_types_set.add(m.entity_type)
                    except Exception:
                        pass
                    pos += field_len

            offset += total_msg_len

        pii_types = sorted(pii_types_set)
        return bool(pii_types), pii_types

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    async def _cache_get(self, fingerprint: str) -> tuple[bytes | None, str | None]:
        """Look up fingerprint in L1, then L2. Returns (data, tier)."""
        if self._l1 is not None:
            result = await self._l1.get(fingerprint)
            if result.hit and result.data is not None:
                return result.data, "l1"

        if self._l2 is not None:
            result = await self._l2.get(fingerprint)
            if result.hit and result.data is not None:
                # Promote to L1
                if self._l1 is not None:
                    await self._l1.put(fingerprint, result.data)
                return result.data, "l2"

        return None, None

    async def _cache_put(
        self,
        fingerprint: str,
        data: bytes,
        *,
        source_id: str | None = None,
        tables: list[str] | None = None,
    ) -> None:
        """Store response bytes in L1 and L2."""
        if self._l1 is not None:
            await self._l1.put(fingerprint, data)
        if self._l2 is not None:
            await self._l2.put(fingerprint, data)
        if self._cache_invalidator is not None and source_id:
            await self._cache_invalidator.record_dependency(
                fingerprint,
                source_id=source_id,
                tables=tables or [],
                protocol="pg",
                metadata={"source_id": source_id, "protocol": "pg", "tables": tables or []},
            )

    def _pipeline(self) -> GatewayPipeline:
        return GatewayPipeline(
            auth_manager=self._auth,
            policy_engine=self._policy,
            rate_limiter=self._rate_limiter,
            audit_logger=self._audit,
            write_classifier=self._write_classifier,
            approval_queue=self._approval_queue,
            source_role_evaluator=self._source_role_evaluator,
            cache_invalidator=self._cache_invalidator,
            cache_barrier_strict=self._cache_barrier_strict,
            sql_naming=self._sql_naming,
        )

    async def _source_generation(self, source_id: str) -> int:
        return await self._pipeline().source_generation(source_id)

    async def _invalidate_caches_after_write(
        self,
        source_id: str,
        tables: list[str],
        *,
        source_generation: int | None = None,
    ) -> None:
        """Conservatively invalidate deterministic caches after writes."""
        try:
            if self._cache_invalidator is not None:
                if tables or source_generation is not None:
                    kwargs = (
                        {"source_generation": source_generation}
                        if source_generation is not None
                        else {}
                    )
                    await self._cache_invalidator.invalidate_for_tables(
                        source_id,
                        tables,
                        **kwargs,
                    )
                else:
                    await self._cache_invalidator.invalidate_for_source(source_id)
                return

            if self._l1 is not None:
                await self._l1.clear()
            if self._l2 is not None:
                await self._l2.clear()

            # Some adaptive strategies own the L1/L2 instances directly.
            # Clear those as well when present; semantic dependency tracking
            # is handled by the cache invalidation service in the fuller path.
            strategy_l1 = getattr(self._cache_strategy, "_l1", None)
            if strategy_l1 is not None and strategy_l1 is not self._l1:
                await strategy_l1.clear()
            strategy_l2 = getattr(self._cache_strategy, "_l2", None)
            if strategy_l2 is not None and strategy_l2 is not self._l2:
                await strategy_l2.clear()
        except Exception:
            logger.warning(
                "Failed to invalidate caches after PG write source=%s tables=%s",
                source_id,
                tables,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------

    def _strategy_for(self, source_id: str | None) -> CacheStrategy | None:
        """The cache strategy this source is configured to use.

        A source set to `bypass` must not be served from cache; sharing one
        strategy across every source is what made that setting inert.
        """
        if self._cache_strategies is None:
            return self._cache_strategy
        config = self._registry.get(source_id) if (self._registry and source_id) else None
        strategy: CacheStrategy = self._cache_strategies.for_source(config)
        return strategy

    async def _emit_audit(
        self,
        sql: str,
        fingerprint: str | None,
        cache_hit: bool,
        cache_tier: str | None,
        latency_ms: float,
        pii_detected: bool = False,
        pii_types: list[str] | None = None,
        source_id: str | None = None,
        identity: IdentityContext | None = None,
        operation: str = "read",
        decision: GatewayDecision | None = None,
    ) -> None:
        """Emit an audit record through the pipeline durability contract."""
        if self._audit is None:
            return
        await self._pipeline().audit(
            GatewayRequest(
                protocol="postgresql",
                source_id=source_id or self.normalizer_source_id,
                identity=identity,
                operation=operation,
                sql=sql,
            ),
            decision=decision,
            fingerprint=fingerprint,
            cache_hit=cache_hit,
            cache_tier=cache_tier,
            latency_ms=latency_ms,
            pii_detected=pii_detected,
            pii_types=pii_types or [],
        )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _safe_close(writer: asyncio.StreamWriter) -> None:
    """Close a StreamWriter, ignoring errors on already-closed transports."""
    with suppress(Exception):
        writer.close()


def _role_context(identity: IdentityContext | None) -> str | None:
    if identity is None:
        return None
    return identity.mapped_pg_role or (identity.roles[0] if identity.roles else None)


def _grants_version(identity: IdentityContext | None) -> str | None:
    if identity is None:
        return None
    if identity.grants_version:
        return str(identity.grants_version)
    metadata = getattr(identity, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("grants_version"):
        return str(metadata["grants_version"])
    return None


def _decision_scope_hash(decision: GatewayDecision | None) -> str | None:
    if decision is None:
        return None
    payload: dict[str, object] = {}
    if decision.policy_decision is not None:
        payload["policy_decision"] = decision.policy_decision.model_dump()
    if decision.source_role_decision is not None:
        source_role = decision.source_role_decision
        payload["source_role_decision"] = (
            source_role.model_dump() if hasattr(source_role, "model_dump") else str(source_role)
        )
    if decision.redaction_required:
        payload["redaction_required"] = True
    if not payload:
        return None
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_error_response(severity: str, code: str, message: str) -> bytes:
    """Build a PG ErrorResponse message.

    Format: 'E' + len + fields. Each field: type_byte + string + \\x00. End: \\x00.
    Severity field: 'S', Code field: 'C', Message field: 'M'.
    """
    fields = bytearray()
    # Severity
    fields.extend(b"S")
    fields.extend(severity.encode("utf-8"))
    fields.extend(b"\x00")
    # SQLSTATE code
    fields.extend(b"C")
    fields.extend(code.encode("utf-8"))
    fields.extend(b"\x00")
    # Message
    fields.extend(b"M")
    fields.extend(message.encode("utf-8"))
    fields.extend(b"\x00")
    # Terminator
    fields.extend(b"\x00")
    return pack_message(MSG_TYPE_ERROR_RESPONSE, bytes(fields))


def _extract_startup_user(startup_data: bytes) -> str | None:
    """Extract the 'user' parameter from a PG startup message."""
    params = _extract_startup_params(startup_data)
    return params.get("user")


def _extract_startup_params(startup_data: bytes) -> dict[str, str]:
    """Extract all key/value parameters from a PG startup message.

    Startup message layout: 4-byte length + 4-byte version + key=val pairs
    separated by NUL bytes, terminated by double NUL.
    """
    if len(startup_data) < 8:
        return {}
    payload = startup_data[8:]
    parts = payload.split(b"\x00")
    result: dict[str, str] = {}
    for i in range(0, len(parts) - 1, 2):
        if i + 1 >= len(parts):
            break
        key = parts[i].decode("utf-8", errors="replace")
        val = parts[i + 1].decode("utf-8", errors="replace")
        if not key:
            break
        result[key] = val
    return result


def _build_startup_message(version: bytes, params: dict[str, str]) -> bytes:
    body = bytearray(version)
    for key, value in params.items():
        if value is None:
            continue
        body.extend(key.encode("utf-8"))
        body.extend(b"\x00")
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\x00")
    body.extend(b"\x00")
    return struct.pack(">I", 4 + len(body)) + bytes(body)


_SAFE_CLIENT_STARTUP_PARAMS = {
    "application_name",
}


def _sanitize_client_startup_params(params: dict[str, str]) -> dict[str, str]:
    """Keep only startup parameters that cannot alter authorization scope.

    PostgreSQL startup parameters can carry libpq ``options`` such as
    ``-c search_path=...`` before the first governed SQL statement. The proxy
    owns governance, so client-controlled GUC/session parameters must not be
    forwarded upstream.
    """

    return {key: value for key, value in params.items() if key in _SAFE_CLIENT_STARTUP_PARAMS}


def _build_upstream_startup(
    client_startup: bytes,
    connection_config: dict[str, object],
    startup_user: str | None,
    startup_db: str | None,
) -> tuple[bytes, str | None, str | None]:
    params = _sanitize_client_startup_params(_extract_startup_params(client_startup))
    upstream_user = (
        _resolve_config_value(
            connection_config,
            "user",
            "username",
            "user_ref",
            "username_ref",
        )
        or startup_user
    )
    upstream_password = _resolve_config_value(
        connection_config,
        "password",
        "password_ref",
    )
    upstream_db = (
        _resolve_config_value(
            connection_config,
            "database",
            "database_ref",
        )
        or startup_db
    )

    if upstream_user:
        params["user"] = upstream_user
    if upstream_db:
        params["database"] = upstream_db

    version = client_startup[4:8] if len(client_startup) >= 8 else struct.pack(">I", 196608)
    return _build_startup_message(version, params), upstream_user, upstream_password


def _resolve_upstream_host_port(
    connection_config: dict[str, object],
    fallback_host: str,
    fallback_port: int,
) -> tuple[str, int]:
    conn_string = _resolve_config_value(
        connection_config,
        "connection_string",
        "connection_string_ref",
    )
    if conn_string:
        parsed = urlparse(conn_string)
        return parsed.hostname or fallback_host, parsed.port or fallback_port

    host = _resolve_config_value(connection_config, "host", "host_ref") or fallback_host
    raw_port = _resolve_config_value(connection_config, "port", "port_ref")
    port = fallback_port if raw_port is None else int(raw_port)
    return str(host), port


class UpstreamConnectionRefusedError(ConnectionError):
    """InterLock declined to use this upstream; the message is safe for the client."""


async def _open_upstream_connection(
    host: str,
    port: int,
    connection_config: dict[str, object],
    *,
    allow_insecure_tls: bool = True,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a PostgreSQL upstream connection, including PG SSL negotiation."""
    # Judged once, by the same function the connector path calls, and before a
    # socket is opened. This check used to live inline here and nowhere else,
    # so MCP and HTTP served sources this path refused.
    refusal = upstream_tls_refusal(connection_config, allow_insecure_tls=allow_insecure_tls)
    if refusal:
        raise UpstreamConnectionRefusedError(refusal)

    ssl_mode = connection_config.get("ssl") or connection_config.get("sslmode")
    if not ssl_mode or str(ssl_mode).lower() in {"0", "false", "no", "off", "disable"}:
        reader, writer = await asyncio.open_connection(host, port)
        return reader, writer

    reader, writer = await asyncio.open_connection(host, port)

    writer.write(struct.pack("!II", 8, SSL_REQUEST_CODE))
    await writer.drain()
    response = await reader.readexactly(1)
    if response != b"S":
        _safe_close(writer)
        raise UpstreamConnectionRefusedError("Upstream PostgreSQL server refused SSL")

    ssl_ca = _resolve_config_value(
        connection_config,
        "ssl_ca",
        "sslrootcert",
        "ssl_ca_ref",
        "sslrootcert_ref",
    )
    context = ssl.create_default_context(cafile=ssl_ca)
    verify = connection_config.get("ssl_verify", connection_config.get("verify_ssl", True))
    if str(verify).lower() in {"0", "false", "no", "off"}:
        # Permitted only outside production; `upstream_tls_refusal` above has
        # already rejected this posture when it is not.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    server_hostname = (
        _resolve_config_value(connection_config, "ssl_server_hostname", "server_hostname") or host
    )
    await writer.start_tls(context, server_hostname=server_hostname)
    return reader, writer


def _resolve_config_value(config: dict[str, object], *keys: str) -> str | None:
    """Thin alias over the shared reader in `connections.source_config`.

    Kept as a local name because this module calls it in a dozen places, but
    the implementation is shared so the PG-wire path and the
    ConnectionManager path cannot drift apart again. They had: this one
    resolved secret references and accepted `sslmode`, and the other did
    neither, so one stored source behaved differently per protocol.
    """
    return resolve_config_value(config, *keys)


def _reader_has_buffered_data(reader: asyncio.StreamReader) -> bool:
    """Return whether an opportunistic drain can start without a timed read.

    Once a frame read starts, a timeout is connection-fatal because cancelling
    ``readexactly`` may have consumed the frame header.  This private-buffer
    check is deliberately limited to the optional drain path; required protocol
    responses still perform a normal bounded read.
    """

    return bool(getattr(reader, "_buffer", b""))


def _read_cstring(payload: bytes, offset: int = 0) -> tuple[str, int]:
    end = payload.find(b"\x00", offset)
    if end < 0:
        return "", len(payload)
    return payload[offset:end].decode("utf-8", errors="replace"), end + 1


def _parse_statement_payload(payload: bytes) -> tuple[str, str]:
    statement_name, offset = _read_cstring(payload)
    sql, _ = _read_cstring(payload, offset)
    return statement_name, sql


def _parse_bind_payload(payload: bytes) -> tuple[str, str]:
    portal_name, offset = _read_cstring(payload)
    statement_name, _ = _read_cstring(payload, offset)
    return portal_name, statement_name


def _parse_execute_payload(payload: bytes) -> str:
    portal_name, _ = _read_cstring(payload)
    return portal_name


def _parse_close_payload(payload: bytes) -> tuple[str, str]:
    if not payload:
        return "", ""
    close_kind = chr(payload[0])
    close_name, _ = _read_cstring(payload, 1)
    return close_kind, close_name


def _apply_close_payload(
    payload: bytes,
    prepared_statements: dict[str, str],
    prepared_statement_decisions: dict[str, GatewayDecision],
    blocked_prepared_statements: set[str],
    portals: dict[str, str],
    blocked_portals: set[str],
) -> None:
    close_kind, close_name = _parse_close_payload(payload)
    if close_kind == "S":
        prepared_statements.pop(close_name, None)
        prepared_statement_decisions.pop(close_name, None)
        blocked_prepared_statements.discard(close_name)
        for portal_name, statement_name in list(portals.items()):
            if statement_name == close_name:
                portals.pop(portal_name, None)
                blocked_portals.discard(portal_name)
    elif close_kind == "P":
        portals.pop(close_name, None)
        blocked_portals.discard(close_name)


def _is_copy_sql(sql: str) -> bool:
    return sql.lstrip().upper().startswith("COPY")


def _protocol_error_message(exc: BaseException) -> str:
    if isinstance(exc, PGFrameTooLargeError):
        return str(exc)
    return "Invalid PostgreSQL protocol message"


def _pg_md5_password(password: str, username: str, salt: bytes) -> bytes:
    # PostgreSQL's legacy MD5 wire authentication requires this exact digest.
    # It is protocol compatibility only; InterLock identity secrets use modern
    # password/API-key hashing elsewhere.
    inner = hashlib.md5(
        (password + username).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    outer = hashlib.md5(inner.encode("ascii") + salt, usedforsecurity=False).hexdigest()
    return f"md5{outer}".encode("ascii")


def _parse_sasl_mechanisms(payload: bytes) -> list[str]:
    return [part.decode("ascii", errors="ignore") for part in payload.split(b"\x00") if part]


def _scram_escape_username(username: str) -> str:
    return username.replace("=", "=3D").replace(",", "=2C")


def _scram_initial_response(username: str) -> tuple[dict[str, str], bytes]:
    nonce = base64.b64encode(os.urandom(18)).decode("ascii").rstrip("=")
    client_first_bare = f"n={_scram_escape_username(username)},r={nonce}"
    client_first = f"n,,{client_first_bare}"
    mechanism = b"SCRAM-SHA-256"
    initial = client_first.encode("utf-8")
    payload = mechanism + b"\x00" + struct.pack(">I", len(initial)) + initial
    return {
        "client_nonce": nonce,
        "client_first_bare": client_first_bare,
    }, payload


def _parse_scram_attrs(message: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for part in message.split(","):
        if len(part) >= 3 and part[1] == "=":
            attrs[part[0]] = part[2:]
    return attrs


def _hi(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _scram_final_response(
    state: dict[str, str],
    server_first: str,
    password: str,
) -> bytes:
    attrs = _parse_scram_attrs(server_first)
    server_nonce = attrs.get("r", "")
    if not server_nonce.startswith(state["client_nonce"]):
        raise ValueError("SCRAM server nonce does not extend client nonce")
    salt = base64.b64decode(attrs["s"])
    iterations = int(attrs["i"])
    client_final_without_proof = f"c=biws,r={server_nonce}"
    auth_message = f"{state['client_first_bare']},{server_first},{client_final_without_proof}"

    salted_password = _hi(password, salt, iterations)
    client_key = _hmac(salted_password, "Client Key")
    stored_key = _sha256(client_key)
    client_signature = _hmac(stored_key, auth_message)
    client_proof = bytes(a ^ b for a, b in zip(client_key, client_signature, strict=True))
    server_key = _hmac(salted_password, "Server Key")
    server_signature = _hmac(server_key, auth_message)
    state["server_signature"] = base64.b64encode(server_signature).decode("ascii")

    proof = base64.b64encode(client_proof).decode("ascii")
    return f"{client_final_without_proof},p={proof}".encode()


def _scram_verify_server_final(state: dict[str, str], server_final: str) -> None:
    attrs = _parse_scram_attrs(server_final)
    verifier = attrs.get("v")
    if verifier and verifier != state.get("server_signature"):
        raise ValueError("SCRAM server signature verification failed")


def _max_source_generation(executions: list[ExtendedExecutionContext]) -> int | None:
    generations = [
        execution.decision.source_generation
        for execution in executions
        if execution.decision is not None and execution.decision.source_generation is not None
    ]
    return max(generations) if generations else None


def _classify_operation(operation: str | None, sql: str) -> str:
    """Classify a SQL statement as 'read' or 'write' based on normalized operation."""
    if operation:
        op_upper = operation.upper()
        if op_upper in (
            "INSERT",
            "UPDATE",
            "DELETE",
            "DROP",
            "TRUNCATE",
            "ALTER",
            "CREATE",
            "EXECUTE",
            "DO",
        ):
            return "write"
        return "read"
    # Fallback: check raw SQL prefix
    stripped = sql.strip().upper()
    for prefix in (
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "TRUNCATE",
        "ALTER",
        "CREATE",
        "EXECUTE",
        "DO",
    ):
        if stripped.startswith(prefix):
            return "write"
    return "read"
