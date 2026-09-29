"""Reverse proxy for HTTP/REST data sources.

Routes incoming requests through /{source_id}/{path} to the upstream
URL configured in each data source's ``base_url``.  JSON responses are
PII-scanned before being returned to the caller.  Non-JSON payloads are
passed through unchanged.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import posixpath
import re
import time
from base64 import b64encode
from collections.abc import AsyncIterator
from hashlib import sha256
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from interlock.cache.provenance import redaction_of, with_redaction
from interlock.connections.source_config import config_bool
from interlock.errors import CacheBarrierUnavailableError
from interlock.gateway.pipeline import (
    GatewayPipeline,
    GatewayRequest,
    bearer_token_from_headers,
    current_cache_source_generation,
)
from interlock.models import IdentityContext
from interlock.security.egress import (
    EgressBlockedError,
    build_safe_async_http_transport,
    validate_http_egress_url,
)

logger = logging.getLogger(__name__)

_DEFAULT_HTTP_MAX_REQUEST_BYTES = 10 * 1_048_576
_DEFAULT_HTTP_MAX_RESPONSE_BYTES = 10 * 1_048_576
_MAX_CONFIGURED_HTTP_BYTES = 64 * 1_048_576
_STREAM_REDACTION_FAILURE = "[REDACTED:SCAN_FAILED]"

_DEFAULT_REQUEST_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "accept-language",
        "content-type",
        "if-match",
        "if-none-match",
        "if-modified-since",
        "if-unmodified-since",
        "idempotency-key",
    }
)
_DEFAULT_RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "cache-control",
        "content-type",
        "etag",
        "expires",
        "last-modified",
        "vary",
    }
)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class _HTTPIngressError(Exception):
    """Deterministic client-facing ingress/proxy error."""

    def __init__(self, status_code: int, error: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error


class HTTPProxy:
    """Reverse proxy for HTTP/REST data sources.

    Routes: /proxy/{source_id}/{path:path}
    Forwards requests to the upstream URL configured in the data source.
    Applies PII scanning to JSON responses.
    """

    def __init__(
        self,
        *,
        max_request_bytes: int = _DEFAULT_HTTP_MAX_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_HTTP_MAX_RESPONSE_BYTES,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        timeout_seconds: float = 30.0,
        request_header_allowlist: set[str] | frozenset[str] | None = None,
        response_header_allowlist: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._client: httpx.AsyncClient | None = None
        self._private_client: httpx.AsyncClient | None = None
        self._max_connections = max(1, max_connections)
        self._max_keepalive_connections = max(0, min(max_keepalive_connections, max_connections))
        self._timeout_seconds = max(0.1, timeout_seconds)
        self._max_request_bytes = _clamp_int(
            max_request_bytes,
            default=_DEFAULT_HTTP_MAX_REQUEST_BYTES,
            minimum=1,
            maximum=_MAX_CONFIGURED_HTTP_BYTES,
        )
        self._max_response_bytes = _clamp_int(
            max_response_bytes,
            default=_DEFAULT_HTTP_MAX_RESPONSE_BYTES,
            minimum=1,
            maximum=_MAX_CONFIGURED_HTTP_BYTES,
        )
        self._request_header_allowlist = _normalize_header_allowlist(
            request_header_allowlist,
            default=_DEFAULT_REQUEST_HEADER_ALLOWLIST,
        )
        self._response_header_allowlist = _normalize_header_allowlist(
            response_header_allowlist,
            default=_DEFAULT_RESPONSE_HEADER_ALLOWLIST,
        )

    async def initialize(self) -> None:
        """Create the httpx AsyncClient."""
        limits = httpx.Limits(
            max_connections=self._max_connections,
            max_keepalive_connections=self._max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            limits=limits,
            transport=build_safe_async_http_transport(limits=limits),
            trust_env=False,
        )
        self._private_client = httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            limits=limits,
            transport=build_safe_async_http_transport(allow_private=True, limits=limits),
            trust_env=False,
        )

    async def shutdown(self) -> None:
        """Close the httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._private_client:
            await self._private_client.aclose()
            self._private_client = None

    def get_routes(self) -> list[Route]:
        """Return Starlette routes for the HTTP proxy."""
        return [
            Route(
                "/proxy/{source_id:str}/{path:path}",
                self.proxy_request,
                methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
            ),
        ]

    async def _authenticate_request(self, request: Request) -> IdentityContext | None:
        """Extract API key from Authorization header and authenticate."""
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        api_key = auth_header[7:]
        state = getattr(getattr(request, "app", None), "state", None)
        auth_mgr = getattr(state, "auth_manager", None) if state else None
        if auth_mgr is None:
            return None  # Auth not configured
        try:
            return await auth_mgr.authenticate(api_key)
        except Exception:
            return None

    async def proxy_request(self, request: Request) -> Response:
        """Forward request to upstream, apply PII scanning, cache GETs."""
        started = time.monotonic()
        state = getattr(getattr(request, "app", None), "state", None)
        pipeline = GatewayPipeline.from_state(state)
        source_id = request.path_params["source_id"]
        path = request.path_params["path"]
        operation = "read" if request.method.upper() in {"GET", "HEAD"} else "write"
        cache_key: str | None = None
        try:
            _validate_request_encoding(request)
            _validate_relative_proxy_path(path)
            body = await _read_bounded_request_body(
                request,
                max_bytes=self._max_request_bytes,
            )
        except _HTTPIngressError as exc:
            return JSONResponse({"error": exc.error}, status_code=exc.status_code)

        gateway_request = GatewayRequest(
            protocol="http",
            source_id=source_id,
            operation=operation,
            method=request.method.upper(),
            path=path,
            parameters=dict(request.query_params),
            metadata={
                "route": str(request.url.path),
                "headers": _safe_approval_headers(request.headers),
                "body": _approval_body_metadata(body),
            },
        )
        decision = await pipeline.preflight(
            gateway_request,
            bearer_token=bearer_token_from_headers(request.headers),
        )
        if not decision.allowed:
            await pipeline.audit(
                gateway_request,
                decision=decision,
                latency_ms=(time.monotonic() - started) * 1000,
                status=(
                    "queued"
                    if decision.approval_state == "pending"
                    else ("denied" if decision.status_code in (403, 429) else "error")
                ),
                error_message=decision.reason,
            )
            return JSONResponse(
                {
                    "error": decision.reason,
                    "approval_id": decision.approval_id,
                    "approval_state": decision.approval_state,
                },
                status_code=decision.status_code,
            )
        identity = decision.identity

        # Look up data source from registry
        registry = getattr(request.app.state, "registry", None)
        if not registry:
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=None,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message="Registry not available",
                metadata={"protocol": "http", "path": path, "error_stage": "source_registry"},
                decision=decision,
            )
            return JSONResponse({"error": "Registry not available"}, status_code=503)

        source = registry.get(source_id)
        if not source:
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=None,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message=f"Unknown source: {source_id}",
                metadata={"protocol": "http", "path": path, "error_stage": "source_lookup"},
                decision=decision,
            )
            return JSONResponse({"error": f"Unknown source: {source_id}"}, status_code=404)

        base_url = source.base_url
        if not base_url:
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=None,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message=f"Source {source_id} has no base_url",
                metadata={"protocol": "http", "path": path, "error_stage": "source_config"},
                decision=decision,
            )
            return JSONResponse({"error": f"Source {source_id} has no base_url"}, status_code=400)

        # Build upstream URL under the configured base path without allowing
        # traversal or absolute-URL smuggling in the path parameter.
        try:
            upstream_url = _build_upstream_url(base_url, path)
        except _HTTPIngressError as exc:
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=None,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message=exc.error,
                metadata={"protocol": "http", "path": path, "error_stage": "url_build"},
                decision=decision,
            )
            return JSONResponse({"error": exc.error}, status_code=exc.status_code)
        try:
            validate_http_egress_url(
                upstream_url,
                allow_private=config_bool(source.connection_config.get("allow_private_egress")),
            )
        except EgressBlockedError as exc:
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=None,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="denied",
                error_message=str(exc),
                metadata={"protocol": "http", "path": path, "error_stage": "egress_guard"},
                decision=decision,
            )
            return JSONResponse({"error": "egress_blocked"}, status_code=403)

        # Forward the request
        if self._client is None:
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=None,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message="HTTP proxy not initialized",
                metadata={"protocol": "http", "path": path, "error_stage": "proxy_lifecycle"},
                decision=decision,
            )
            return JSONResponse({"error": "HTTP proxy not initialized"}, status_code=503)

        allow_private_egress = config_bool(source.connection_config.get("allow_private_egress"))
        upstream_client = (
            self._private_client if allow_private_egress and self._private_client else self._client
        )

        circuit_registry = getattr(request.app.state, "circuit_breakers", None)
        circuit_breaker = circuit_registry.get(source_id) if circuit_registry is not None else None
        if circuit_breaker is not None and not circuit_breaker.can_execute():
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=None,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message=f"Circuit breaker is open for source: {source_id}",
                metadata={"protocol": "http", "path": path, "error_stage": "circuit_breaker"},
                decision=decision,
            )
            return JSONResponse(
                {"error": f"Source is temporarily unavailable: {source_id}"},
                status_code=503,
            )

        if request.method.upper() == "GET":
            try:
                source_generation = await current_cache_source_generation(
                    getattr(request.app.state, "cache_invalidator", None),
                    source_id,
                    strict=bool(getattr(request.app.state, "cache_barrier_strict", False)),
                )
            except CacheBarrierUnavailableError:
                return JSONResponse({"error": "cache_consistency_unavailable"}, status_code=503)
            cache_key = _http_cache_key(
                source_id=source_id,
                method=request.method,
                path=path,
                query=str(request.query_params),
                identity_id=(identity.identity_id if identity is not None else None),
                role=(identity.mapped_pg_role if identity is not None else None),
                team=identity.team if identity is not None else None,
                grants_version=_identity_grants_version(identity),
                governance_scope_hash=_http_governance_scope_hash(decision),
                source_generation=source_generation,
            )
            cached = await self._cache_get(request, cache_key, source_id)
            if cached is not None:
                await self._audit(
                    request,
                    identity=identity,
                    source_id=source_id,
                    operation=operation,
                    cache_key=cache_key,
                    cache_hit=True,
                    cache_tier=cached[1],
                    latency_ms=(time.monotonic() - started) * 1000,
                    status="success",
                    # The cached body was redacted when stored; carry that record.
                    metadata={
                        "protocol": "http",
                        "path": path,
                        "redaction_stats": redaction_of(cached[2])[2] or None,
                    },
                    decision=decision,
                )
                return Response(content=cached[0], status_code=200)

        headers = _filter_request_headers(
            request.headers,
            allowlist=self._request_header_allowlist,
        )

        try:
            upstream_request = upstream_client.build_request(
                method=request.method,
                url=upstream_url,
                content=body if body else None,
                headers=headers,
                params=dict(request.query_params),
            )
            _prune_prepared_request_headers(
                upstream_request.headers,
                allowlist=self._request_header_allowlist,
            )
            upstream_response = await upstream_client.send(upstream_request, stream=True)
        except _HTTPIngressError as exc:
            if circuit_breaker is not None:
                circuit_breaker.record_failure()
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=cache_key,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message=exc.error,
                metadata={"protocol": "http", "path": path, "error_stage": "response_body"},
                decision=decision,
            )
            return JSONResponse({"error": exc.error}, status_code=exc.status_code)
        except Exception as exc:
            if circuit_breaker is not None:
                circuit_breaker.record_failure()
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=cache_key,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message="upstream_request_failed",
                metadata={
                    "protocol": "http",
                    "path": path,
                    "upstream_url": upstream_url,
                    "error_stage": "upstream_request",
                },
                decision=decision,
            )
            logger.warning("HTTP upstream request failed: %s", exc.__class__.__name__)
            return JSONResponse({"error": "upstream_request_failed"}, status_code=502)

        content_type = upstream_response.headers.get("content-type", "")
        if _is_streaming_redaction_type(content_type) and request.method.upper() != "HEAD":
            try:
                _validate_declared_response_size(
                    upstream_response,
                    max_bytes=self._max_response_bytes,
                )
            except _HTTPIngressError as exc:
                await upstream_response.aclose()
                if circuit_breaker is not None:
                    circuit_breaker.record_failure()
                await self._audit(
                    request,
                    identity=identity,
                    source_id=source_id,
                    operation=operation,
                    cache_key=cache_key,
                    cache_hit=False,
                    cache_tier=None,
                    latency_ms=(time.monotonic() - started) * 1000,
                    status="error",
                    error_message=exc.error,
                    metadata={
                        "protocol": "http",
                        "path": path,
                        "error_stage": "response_headers",
                    },
                    decision=decision,
                )
                return JSONResponse({"error": exc.error}, status_code=exc.status_code)

            if operation == "write" and upstream_response.status_code < 400:
                await self._invalidate_after_write(
                    request,
                    source_id=source_id,
                    path=path,
                    source_generation=decision.source_generation,
                )

            resp_headers = _filter_response_headers(
                upstream_response.headers,
                allowlist=self._response_header_allowlist,
            )
            policy_redactions = (
                decision.policy_decision.redact_columns if decision.policy_decision else []
            )
            stream_stats: dict[str, Any] = {
                "streamed": True,
                "bytes_in": 0,
                "bytes_out": 0,
                "records": 0,
                "scanner_failures": 0,
            }

            async def stream_body() -> AsyncIterator[bytes]:
                stream_status = "success" if upstream_response.status_code < 400 else "error"
                stream_error: str | None = None
                circuit_failed = upstream_response.status_code >= 500
                try:
                    async for chunk in _iter_bounded_redacted_response(
                        upstream_response,
                        request=request,
                        content_type=content_type,
                        policy_redactions=policy_redactions,
                        max_bytes=self._max_response_bytes,
                        stats=stream_stats,
                    ):
                        yield chunk
                except _HTTPIngressError as exc:
                    stream_status = "error"
                    stream_error = exc.error
                    circuit_failed = True
                    stream_stats["error"] = exc.error
                    # Bytes are already on the wire with a 200 and chunked
                    # framing, so HTTP offers no way to answer 413 now - the
                    # same condition detected *before* streaming does return a
                    # clean 413, and that asymmetry is unavoidable. What is
                    # avoidable is silence: emit an in-band marker naming the
                    # reason, so a truncated stream is diagnosable from its own
                    # body rather than only as a transport error that looks
                    # identical to the upstream crashing.
                    #
                    # The connection is still torn down without a terminating
                    # chunk, deliberately. A clean close would leave a naive
                    # client with a well-formed 200 and a short body, which is
                    # the quieter and worse failure.
                    with contextlib.suppress(Exception):
                        yield f"{_STREAM_REDACTION_FAILURE} {exc.error}\n".encode()
                    raise
                except (asyncio.CancelledError, GeneratorExit):
                    stream_status = "cancelled"
                    stream_error = "downstream_cancelled"
                    stream_stats["cancelled"] = True
                    raise
                except BaseException as exc:
                    stream_status = "error"
                    stream_error = "upstream_response_failed"
                    circuit_failed = True
                    stream_stats["error"] = exc.__class__.__name__
                    raise
                finally:
                    if circuit_breaker is not None:
                        if circuit_failed:
                            circuit_breaker.record_failure()
                        elif stream_status != "cancelled":
                            circuit_breaker.record_success()
                    with anyio.CancelScope(shield=True):
                        await self._audit(
                            request,
                            identity=identity,
                            source_id=source_id,
                            operation=operation,
                            cache_key=cache_key,
                            cache_hit=False,
                            cache_tier=None,
                            latency_ms=(time.monotonic() - started) * 1000,
                            status=stream_status,
                            error_message=stream_error,
                            metadata={
                                "protocol": "http",
                                "path": path,
                                "method": request.method,
                                "status_code": upstream_response.status_code,
                                "upstream_url": upstream_url,
                                "redaction_stats": stream_stats,
                            },
                            decision=decision,
                        )

            return StreamingResponse(
                stream_body(),
                status_code=upstream_response.status_code,
                headers=resp_headers,
            )

        try:
            try:
                response_body = await _read_bounded_upstream_response(
                    upstream_response,
                    max_bytes=self._max_response_bytes,
                )
            finally:
                await upstream_response.aclose()
        except _HTTPIngressError as exc:
            if circuit_breaker is not None:
                circuit_breaker.record_failure()
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=cache_key,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message=exc.error,
                metadata={"protocol": "http", "path": path, "error_stage": "response_body"},
                decision=decision,
            )
            return JSONResponse({"error": exc.error}, status_code=exc.status_code)
        except Exception as exc:
            if circuit_breaker is not None:
                circuit_breaker.record_failure()
            await self._audit(
                request,
                identity=identity,
                source_id=source_id,
                operation=operation,
                cache_key=cache_key,
                cache_hit=False,
                cache_tier=None,
                latency_ms=(time.monotonic() - started) * 1000,
                status="error",
                error_message="upstream_response_failed",
                metadata={
                    "protocol": "http",
                    "path": path,
                    "upstream_url": upstream_url,
                    "error_stage": "response_body",
                },
                decision=decision,
            )
            logger.warning("HTTP upstream response failed: %s", exc.__class__.__name__)
            return JSONResponse({"error": "upstream_response_failed"}, status_code=502)

        if circuit_breaker is not None:
            if upstream_response.status_code >= 500:
                circuit_breaker.record_failure()
            else:
                circuit_breaker.record_success()

        # Ordinary JSON stays buffered so recursive PII and policy redaction
        # complete before any bytes reach the caller or cache.

        if "application/json" in content_type:
            response_body = await self._scan_json_response(request, response_body)
        elif _is_text_like(content_type):
            response_body = await self._scan_text_response(request, response_body)
        policy_redaction_stats: dict[str, Any] | None = None
        policy_redactions = (
            decision.policy_decision.redact_columns if decision.policy_decision else []
        )
        if policy_redactions and "application/json" in content_type:
            response_body, policy_redaction_stats = _apply_policy_json_redaction(
                response_body,
                policy_redactions,
            )

        if cache_key is not None and upstream_response.status_code == 200 and response_body:
            await self._cache_put(
                request,
                cache_key,
                response_body,
                with_redaction(
                    {
                        "source_id": source_id,
                        "protocol": "http",
                        "path": path,
                        "content_type": content_type,
                    },
                    pii_detected=bool(policy_redaction_stats),
                    pii_types=[],
                    redaction_stats=policy_redaction_stats,
                ),
            )

        if operation == "write" and upstream_response.status_code < 400:
            await self._invalidate_after_write(
                request,
                source_id=source_id,
                path=path,
                source_generation=decision.source_generation,
            )

        resp_headers = _filter_response_headers(
            upstream_response.headers,
            allowlist=self._response_header_allowlist,
        )

        await self._audit(
            request,
            identity=identity,
            source_id=source_id,
            operation=operation,
            cache_key=cache_key,
            cache_hit=False,
            cache_tier=None,
            latency_ms=(time.monotonic() - started) * 1000,
            status="success" if upstream_response.status_code < 400 else "error",
            metadata={
                "protocol": "http",
                "path": path,
                "method": request.method,
                "status_code": upstream_response.status_code,
                "upstream_url": upstream_url,
                "redaction_stats": policy_redaction_stats,
            },
            decision=decision,
        )

        return Response(
            content=response_body,
            status_code=upstream_response.status_code,
            headers=resp_headers,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _invalidate_after_write(
        self,
        request: Request,
        *,
        source_id: str,
        path: str,
        source_generation: int | None,
    ) -> None:
        invalidator = getattr(request.app.state, "cache_invalidator", None)
        if invalidator is None:
            return
        try:
            if source_generation is not None:
                await invalidator.invalidate_for_tables(
                    source_id,
                    [],
                    source_generation=source_generation,
                )
            else:
                await invalidator.invalidate_for_source(source_id)
        except Exception:
            logger.warning(
                "HTTP cache invalidation failed source=%s path=%s",
                source_id,
                path,
                exc_info=True,
            )

    async def _scan_json_response(self, request: Request, raw_body: bytes) -> bytes:
        """Run PII scanner over a JSON response body, returning updated bytes."""
        pii_scanner = getattr(request.app.state, "pii_scanner", None)
        if pii_scanner is None:
            return raw_body

        try:
            data: Any = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return raw_body

        try:
            if isinstance(data, list):
                processed = []
                for item in data:
                    if isinstance(item, dict):
                        redacted, _ = await pii_scanner.process_row(item)
                        processed.append(redacted)
                    else:
                        processed.append(item)
                return json.dumps(processed).encode()
            elif isinstance(data, dict):
                redacted, _ = await pii_scanner.process_row(data)
                return json.dumps(redacted).encode()
        except Exception:
            logger.warning("PII scan failed on JSON response; redacting response body")
            return json.dumps({"error": "response redacted after scanner failure"}).encode()

        return raw_body

    async def _scan_text_response(self, request: Request, raw_body: bytes) -> bytes:
        """Best-effort PII redaction for text-like HTTP responses."""
        scanner = getattr(request.app.state, "pii_fast_scanner", None)
        if scanner is None:
            return raw_body
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return raw_body
        try:
            return scanner.redact(text).encode("utf-8")
        except Exception:
            logger.warning("PII scan failed on text response; redacting response body")
            return b"[REDACTED:SCAN_FAILED]"

    def _strategy_for(self, request: Request, source_id: object) -> Any | None:
        """The cache strategy this source is configured to use.

        Falls back to the gateway-wide strategy when no per-source resolver is
        wired, as in unit tests built without the full lifespan.
        """
        state = request.app.state
        resolver = getattr(state, "cache_strategies", None)
        if resolver is None:
            return getattr(state, "cache_strategy", None)
        registry = getattr(state, "registry", None)
        config = registry.get(source_id) if registry is not None and source_id else None
        return resolver.for_source(config)

    async def _cache_get(
        self, request: Request, cache_key: str, source_id: str
    ) -> tuple[bytes, str | None, dict[str, Any]] | None:
        strategy = self._strategy_for(request, source_id)
        if strategy is not None:
            try:
                result = await strategy.get(cache_key)
                if result.hit and result.data is not None:
                    return result.data, result.tier, result.metadata
                # A miss is an answer. Falling through to L2 here would serve
                # a source configured to `bypass` from the shared cache.
                return None
            except Exception:
                logger.debug("HTTP cache strategy get failed", exc_info=True)
        l2 = getattr(request.app.state, "l2_cache", None)
        if l2 is not None:
            result = await l2.get(cache_key)
            if result.hit and result.data is not None:
                return result.data, result.tier, result.metadata
        return None

    async def _cache_put(
        self,
        request: Request,
        cache_key: str,
        data: bytes,
        metadata: dict[str, Any],
    ) -> None:
        strategy = self._strategy_for(request, metadata.get("source_id"))
        if strategy is not None:
            try:
                await strategy.put(cache_key, data, metadata)
                await self._record_cache_dependency(request, cache_key, metadata)
                return
            except Exception:
                logger.debug("HTTP cache strategy put failed", exc_info=True)
        l2 = getattr(request.app.state, "l2_cache", None)
        if l2 is not None:
            await l2.put(cache_key, data, metadata=metadata)
        await self._record_cache_dependency(request, cache_key, metadata)

    async def _record_cache_dependency(
        self,
        request: Request,
        cache_key: str,
        metadata: dict[str, Any],
    ) -> None:
        invalidator = getattr(request.app.state, "cache_invalidator", None)
        source_id = metadata.get("source_id")
        if invalidator is None or not isinstance(source_id, str):
            return
        try:
            await invalidator.record_dependency(
                cache_key,
                source_id=source_id,
                asset_path=metadata.get("path"),
                protocol="http",
                metadata=metadata,
            )
        except Exception:
            logger.debug("HTTP cache dependency record failed", exc_info=True)

    async def _audit(
        self,
        request: Request,
        *,
        identity: IdentityContext | None,
        source_id: str,
        operation: str,
        cache_key: str | None,
        cache_hit: bool,
        cache_tier: str | None,
        latency_ms: float,
        status: str,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        decision: Any | None = None,
    ) -> None:
        state = getattr(getattr(request, "app", None), "state", None)
        pipeline = GatewayPipeline.from_state(state)
        await pipeline.audit(
            GatewayRequest(
                protocol="http",
                source_id=source_id,
                operation=operation,
                identity=identity,
                method=request.method.upper(),
                path=metadata.get("path") if metadata else request.url.path,
                metadata=metadata or {},
            ),
            fingerprint=cache_key,
            cache_hit=cache_hit,
            cache_tier=cache_tier,
            latency_ms=latency_ms,
            status=status,
            error_message=error_message,
            decision=decision,
        )


def _http_cache_key(
    *,
    source_id: str,
    method: str,
    path: str,
    query: str,
    identity_id: int | None,
    role: str | None,
    team: str | None,
    grants_version: str | None,
    governance_scope_hash: str | None,
    source_generation: int | None = None,
) -> str:
    """Return a governance-scoped deterministic HTTP cache key.

    Cached bodies have already passed policy and PII response processing.  The
    key must therefore bind the representation to the complete authorization
    scope that produced it; sharing only by source/team/role can expose a
    differently redacted response to another identity.
    """
    payload = json.dumps(
        {
            "namespace": "http-governed-v3",
            "source_id": source_id,
            "method": method.upper(),
            "path": path,
            "query": query,
            "identity_id": identity_id,
            "role": role,
            "team": team,
            "grants_version": grants_version,
            "governance_scope_hash": governance_scope_hash,
            "source_generation": 0 if source_generation is None else int(source_generation),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _identity_grants_version(identity: IdentityContext | None) -> str | None:
    if identity is None or identity.grants_version is None:
        return None
    return str(identity.grants_version)


def _http_governance_scope_hash(decision: Any | None) -> str | None:
    """Hash the authorization and response-policy decisions for cache scope."""
    if decision is None:
        return None
    payload: dict[str, Any] = {}
    policy_decision = getattr(decision, "policy_decision", None)
    if policy_decision is not None:
        payload["policy_decision"] = (
            policy_decision.model_dump()
            if hasattr(policy_decision, "model_dump")
            else str(policy_decision)
        )
    source_role_decision = getattr(decision, "source_role_decision", None)
    if source_role_decision is not None:
        payload["source_role_decision"] = (
            source_role_decision.model_dump()
            if hasattr(source_role_decision, "model_dump")
            else str(source_role_decision)
        )
    payload["redaction_required"] = bool(getattr(decision, "redaction_required", False))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _is_text_like(content_type: str) -> bool:
    return any(
        marker in content_type
        for marker in ("text/", "application/csv", "text/csv", "application/xml")
    )


def _is_streaming_redaction_type(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type.startswith("text/") or media_type in {
        "application/csv",
        "application/jsonl",
        "application/jsonlines",
        "application/ndjson",
        "application/x-ndjson",
        "application/xml",
    }


def _is_ndjson(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type in {
        "application/jsonl",
        "application/jsonlines",
        "application/ndjson",
        "application/x-ndjson",
    }


def _validate_declared_response_size(response: httpx.Response, *, max_bytes: int) -> None:
    content_length = response.headers.get("content-length")
    if not content_length:
        return
    try:
        declared = int(content_length)
    except ValueError:
        return
    if declared > max_bytes:
        raise _HTTPIngressError(413, "response_too_large")


def _is_csv_like(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type in {"text/csv", "application/csv"}


def _record_boundary(pending: str, *, csv_aware: bool, in_quotes: bool) -> tuple[int, bool]:
    r"""Index just past the next record boundary, and the quote state after it.

    Returns (-1, in_quotes) when no complete record is available yet.

    For newline-delimited content this is just the next newline. For CSV it is
    the next newline *outside* a quoted field, because RFC 4180 permits a
    newline inside one - and a value wrapped across such a break used to be
    cut into fragments that matched no pattern, passing PII through
    unredacted. Three of the six fast-tier patterns can span a newline, since
    their separator classes include `\s`: CREDIT_CARD, PHONE and MRN. SSN and
    EMAIL cannot, which is why the obvious probe value never showed it.

    Quote handling follows RFC 4180: a doubled quote inside a quoted field is
    an escaped quote and does not end it.
    """
    if not csv_aware:
        index = pending.find("\n")
        return (index + 1 if index >= 0 else -1), in_quotes

    position = 0
    length = len(pending)
    while position < length:
        char = pending[position]
        if char == '"':
            if in_quotes and position + 1 < length and pending[position + 1] == '"':
                position += 2  # escaped quote, still inside the field
                continue
            if in_quotes and position + 1 == length:
                # Cannot yet tell an escaped quote from a closing one; wait for
                # the next chunk rather than guessing and splitting a record.
                return -1, in_quotes
            in_quotes = not in_quotes
        elif char == "\n" and not in_quotes:
            return position + 1, in_quotes
        position += 1
    return -1, in_quotes


async def _iter_bounded_redacted_response(
    response: httpx.Response,
    *,
    request: Request,
    content_type: str,
    policy_redactions: list[str],
    max_bytes: int,
    stats: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    """Pull, redact, and emit a bounded line-oriented upstream response.

    Incomplete lines remain private until their continuation arrives, which
    prevents a PII token split across transport chunks from bypassing the
    scanner.  Reading advances only when the ASGI server requests the next
    output chunk, providing natural downstream-to-upstream backpressure.
    """
    stream_stats = stats if stats is not None else {}
    stream_stats.setdefault("bytes_in", 0)
    stream_stats.setdefault("bytes_out", 0)
    stream_stats.setdefault("records", 0)
    stream_stats.setdefault("scanner_failures", 0)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = ""
    csv_aware = _is_csv_like(content_type)
    in_quotes = False
    total_in = 0
    total_out = 0

    async def emit(record: str) -> bytes:
        nonlocal total_out
        redacted = await _redact_stream_record(
            request,
            record,
            content_type=content_type,
            policy_redactions=policy_redactions,
            stats=stream_stats,
        )
        encoded = redacted.encode("utf-8")
        if total_out + len(encoded) > max_bytes:
            raise _HTTPIngressError(413, "response_too_large")
        total_out += len(encoded)
        stream_stats["bytes_out"] = total_out
        stream_stats["records"] = int(stream_stats["records"]) + 1
        return encoded

    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total_in += len(chunk)
            stream_stats["bytes_in"] = total_in
            if total_in > max_bytes:
                raise _HTTPIngressError(413, "response_too_large")
            try:
                pending += decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                stream_stats["scanner_failures"] = int(stream_stats["scanner_failures"]) + 1
                marker = f"{_STREAM_REDACTION_FAILURE}\n".encode()
                if total_out + len(marker) > max_bytes:
                    raise _HTTPIngressError(413, "response_too_large") from exc
                stream_stats["bytes_out"] = total_out + len(marker)
                yield marker
                return

            while True:
                boundary, boundary_quotes = _record_boundary(
                    pending, csv_aware=csv_aware, in_quotes=in_quotes
                )
                if boundary < 0:
                    # Keep the existing quote state. The scan that just
                    # returned -1 walked the whole pending buffer, but none of
                    # it was consumed, so it will be rescanned from the start
                    # when the next chunk arrives - adopting the end-of-buffer
                    # state here would apply those quotes twice and invert it.
                    # That is why a CSV body redacted correctly in one chunk
                    # and leaked when split across two.
                    break
                record = pending[:boundary]
                pending = pending[boundary:]
                in_quotes = boundary_quotes
                yield await emit(record)

        try:
            pending += decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            stream_stats["scanner_failures"] = int(stream_stats["scanner_failures"]) + 1
            pending = f"{_STREAM_REDACTION_FAILURE}\n"
        if pending:
            yield await emit(pending)
        stream_stats["completed"] = True
    finally:
        await response.aclose()


def _ndjson_failure(reason: str) -> str:
    """A dropped ndjson record, carrying the same marker as every other format.

    ndjson failures used to emit only `{"error": ...}`, so anything detecting a
    dropped record by the `[REDACTED:SCAN_FAILED]` marker - a log rule, a
    client-side check, a downstream scan - saw text and csv failures and missed
    ndjson ones entirely. One token means one detection rule covers every
    streaming format, and the JSON shape is kept so a consumer parsing the
    stream still gets valid ndjson.
    """
    return json.dumps({"error": reason, "marker": _STREAM_REDACTION_FAILURE})


async def _redact_stream_record(
    request: Request,
    record: str,
    *,
    content_type: str,
    policy_redactions: list[str],
    stats: dict[str, Any],
) -> str:
    if _is_ndjson(content_type):
        payload, ending = _split_line_ending(record)
        if not payload.strip():
            return record
        try:
            data: Any = json.loads(payload)
        except json.JSONDecodeError:
            stats["scanner_failures"] = int(stats["scanner_failures"]) + 1
            return _ndjson_failure("invalid upstream JSON record") + ending
        if not isinstance(data, dict):
            stats["scanner_failures"] = int(stats["scanner_failures"]) + 1
            return _ndjson_failure("invalid upstream JSON record") + ending

        pii_scanner = getattr(request.app.state, "pii_scanner", None)
        if pii_scanner is not None:
            try:
                data, _ = await pii_scanner.process_row(data)
            except Exception:
                logger.warning("PII scan failed on streamed JSON record; redacting record")
                stats["scanner_failures"] = int(stats["scanner_failures"]) + 1
                return _ndjson_failure("response redacted after scanner failure") + ending
        if policy_redactions:
            redacted, policy_stats = _apply_policy_json_redaction(
                json.dumps(data).encode("utf-8"),
                policy_redactions,
            )
            data = json.loads(redacted)
            stats["policy_redacted_fields"] = sorted(
                set(stats.get("policy_redacted_fields", []))
                | set(policy_stats.get("redacted_fields", []))
            )
        return json.dumps(data, separators=(",", ":")) + ending

    scanner = getattr(request.app.state, "pii_fast_scanner", None)
    if scanner is None:
        return record
    try:
        return str(scanner.redact(record))
    except Exception:
        logger.warning("PII scan failed on streamed text record; redacting record")
        stats["scanner_failures"] = int(stats["scanner_failures"]) + 1
        _, ending = _split_line_ending(record)
        return _STREAM_REDACTION_FAILURE + ending


def _split_line_ending(record: str) -> tuple[str, str]:
    if record.endswith("\r\n"):
        return record[:-2], "\r\n"
    if record.endswith("\n"):
        return record[:-1], "\n"
    return record, ""


async def _read_bounded_request_body(request: Request, *, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise _HTTPIngressError(400, "invalid_content_length") from exc
        if declared < 0:
            raise _HTTPIngressError(400, "invalid_content_length")
        if declared > max_bytes:
            raise _HTTPIngressError(413, "request_too_large")

    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise _HTTPIngressError(413, "request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_bounded_upstream_response(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> bytes:
    _validate_declared_response_size(response, max_bytes=max_bytes)

    total = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise _HTTPIngressError(413, "response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_request_encoding(request: Request) -> None:
    encoding = request.headers.get("content-encoding")
    if encoding and encoding.lower().strip() not in {"identity"}:
        raise _HTTPIngressError(422, "unsupported_content_encoding")


def _build_upstream_url(base_url: str, path: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise _HTTPIngressError(422, "invalid_source_base_url")
    if parts.query or parts.fragment:
        raise _HTTPIngressError(422, "invalid_source_base_url")

    base_path = _normalize_base_path(parts.path)
    relative_path = _safe_encoded_relative_path(path)
    if relative_path:
        upstream_path = f"{base_path.rstrip('/')}/{relative_path}" if base_path else relative_path
    else:
        upstream_path = base_path or "/"
    if not upstream_path.startswith("/"):
        upstream_path = f"/{upstream_path}"

    if (
        base_path
        and upstream_path != base_path
        and not upstream_path.startswith(f"{base_path.rstrip('/')}/")
    ):
        raise _HTTPIngressError(400, "invalid_path")
    return urlunsplit((parts.scheme, parts.netloc, upstream_path, "", ""))


def _normalize_base_path(path: str) -> str:
    if not path or path == "/":
        return ""
    safe = _safe_encoded_relative_path(path.strip("/"))
    normalized = posixpath.normpath(f"/{safe}") if safe else ""
    if normalized in {"/.", "/.."} or normalized.startswith("/../"):
        raise _HTTPIngressError(422, "invalid_source_base_url")
    return "" if normalized == "/" else normalized


def _validate_relative_proxy_path(path: str) -> None:
    _safe_encoded_relative_path(path)


def _safe_encoded_relative_path(path: str) -> str:
    if path.startswith("/"):
        raise _HTTPIngressError(400, "invalid_path")
    if "\x00" in path or "\\" in path:
        raise _HTTPIngressError(400, "invalid_path")
    if not path:
        return ""

    encoded_segments: list[str] = []
    for segment in path.split("/"):
        if not segment:
            raise _HTTPIngressError(400, "invalid_path")
        try:
            decoded = unquote(segment)
        except ValueError as exc:
            raise _HTTPIngressError(400, "invalid_path") from exc
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded or "\x00" in decoded:
            raise _HTTPIngressError(400, "invalid_path")
        encoded_segments.append(quote(decoded, safe="-._~!$&'()*+,;=:@"))
    return "/".join(encoded_segments)


def _filter_request_headers(headers, *, allowlist: frozenset[str]) -> dict[str, str]:
    output: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP_HEADERS:
            continue
        if lowered in {"authorization", "cookie", "host", "content-length"}:
            continue
        if lowered in allowlist:
            output[lowered] = value
    return output


def _prune_prepared_request_headers(headers: httpx.Headers, *, allowlist: frozenset[str]) -> None:
    for key in list(headers.keys()):
        lowered = key.lower()
        if lowered in {"host", "content-length"}:
            continue
        if lowered in _HOP_BY_HOP_HEADERS:
            del headers[key]
            continue
        if lowered in {"authorization", "cookie"} or lowered not in allowlist:
            del headers[key]


def _filter_response_headers(headers, *, allowlist: frozenset[str]) -> dict[str, str]:
    output: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP_HEADERS:
            continue
        if lowered in {"content-length", "content-encoding", "location", "set-cookie"}:
            continue
        if lowered in allowlist:
            output[lowered] = value
    return output


def _normalize_header_allowlist(
    override: set[str] | frozenset[str] | None,
    *,
    default: frozenset[str],
) -> frozenset[str]:
    if override is None:
        values = default
    else:
        values = frozenset(str(header).lower() for header in override)
    return frozenset(header for header in values if header not in _HOP_BY_HOP_HEADERS)


def _clamp_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        candidate = default
    return max(minimum, min(candidate, maximum))


def _safe_approval_headers(headers) -> dict[str, str]:
    allowed = {
        "accept",
        "content-type",
        "if-match",
        "if-none-match",
        "idempotency-key",
    }
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}


_SECRET_FIELD_MARKERS = (
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "cookie",
    "credential",
)
_MAX_AUDIT_BODY_CHARS = 4096


def _approval_body_metadata(body: bytes) -> dict[str, object] | None:
    if not body:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "data": b64encode(body[:_MAX_AUDIT_BODY_CHARS]).decode("ascii"),
            "truncated": len(body) > _MAX_AUDIT_BODY_CHARS,
        }

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        redacted_text = _redact_secret_markers(text)
        return {
            "encoding": "text",
            "data": redacted_text[:_MAX_AUDIT_BODY_CHARS],
            "truncated": len(redacted_text) > _MAX_AUDIT_BODY_CHARS,
            "original_bytes": len(body),
        }

    redacted = _redact_json_secrets(parsed)
    if len(text) > _MAX_AUDIT_BODY_CHARS:
        return {
            "encoding": "truncated",
            "data": json.dumps(redacted)[:_MAX_AUDIT_BODY_CHARS],
            "truncated": True,
            "original_bytes": len(body),
            "content_type": "application/json",
        }
    return {
        "encoding": "json",
        "data": redacted,
        "truncated": len(text) > _MAX_AUDIT_BODY_CHARS,
        "original_bytes": len(body),
    }


def _redact_json_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if _looks_secret_key(str(key)) else _redact_json_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_secrets(item) for item in value]
    return value


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in _SECRET_FIELD_MARKERS)


def _redact_secret_markers(text: str) -> str:
    redacted = text
    for marker in _SECRET_FIELD_MARKERS:
        redacted = re.sub(
            rf"(?i)({re.escape(marker)}\s*[=:]\s*)([^\s&;,]+)",
            r"\1[REDACTED]",
            redacted,
        )
    return redacted


def _apply_policy_json_redaction(
    raw_body: bytes,
    fields: list[str],
) -> tuple[bytes, dict[str, Any]]:
    field_set = {field.lower() for field in fields}
    if not field_set:
        return raw_body, {"policy_fields": [], "redacted_fields": [], "count": 0}
    try:
        data = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw_body, {"policy_fields": sorted(field_set), "redacted_fields": [], "count": 0}

    redacted_fields: set[str] = set()
    count = 0

    def _walk(value: Any) -> Any:
        nonlocal count
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for key, item in value.items():
                if str(key).lower() in field_set:
                    output[key] = "[REDACTED:POLICY]"
                    redacted_fields.add(str(key))
                    count += 1
                else:
                    output[key] = _walk(item)
            return output
        if isinstance(value, list):
            return [_walk(item) for item in value]
        return value

    redacted = _walk(data)
    return json.dumps(redacted).encode("utf-8"), {
        "policy_fields": sorted(field_set),
        "redacted_fields": sorted(redacted_fields),
        "count": count,
    }
