"""Control-plane health/readiness helpers."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from interlock.errors import DependencyNotReadyError
from interlock.observability.otel import get_meter

CORRELATION_HEADER = "X-Correlation-ID"

CheckResult = dict[str, Any]
DependencyCheck = Callable[[], Awaitable[Mapping[str, Any] | None] | Mapping[str, Any] | None]

_HEALTH_COUNTER: object | None = None
_HEALTH_DURATION: object | None = None


def correlation_id_from_request(request: Request) -> str:
    """Return a bounded inbound correlation id or create a new one."""
    raw = request.headers.get("x-correlation-id") or request.headers.get("x-request-id")
    if raw:
        normalized = "".join(ch for ch in raw.strip() if ch.isprintable() and ch not in "\r\n")
        if normalized:
            return normalized[:128]
    return str(uuid.uuid4())


async def run_dependency_checks(
    checks: Mapping[str, DependencyCheck],
    *,
    timeout_seconds: float,
) -> tuple[bool, dict[str, CheckResult]]:
    """Run dependency probes and return aggregate readiness plus sanitized results."""
    results: dict[str, CheckResult] = {}
    ready = True
    for name, check in checks.items():
        started = time.monotonic()
        try:
            value = check()
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, timeout=timeout_seconds)
            latency_ms = (time.monotonic() - started) * 1000
            detail: CheckResult = {"status": "ok", "latency_ms": round(latency_ms, 3)}
            if isinstance(value, Mapping):
                detail.update(dict(value))
            results[name] = detail
        except Exception as exc:
            ready = False
            latency_ms = (time.monotonic() - started) * 1000
            detail_error: CheckResult = {
                "status": "error",
                "latency_ms": round(latency_ms, 3),
                "error": type(exc).__name__,
            }
            # Surface the message only for our own readiness errors, whose text
            # is written to be read by an operator. An arbitrary exception can
            # carry internals - a DSN inside a connection error, for instance -
            # so those stay reduced to a type name.
            #
            # Without this every failing check reported only
            # "DependencyNotReadyError", which for the audit check meant an
            # operator could not tell which of four conditions had fired, nor a
            # transient backlog from a permanent one.
            if isinstance(exc, DependencyNotReadyError):
                detail_error["reason"] = str(exc)
            results[name] = detail_error
    return ready, results


def control_plane_response(
    request: Request,
    payload: Mapping[str, Any],
    *,
    status_code: int,
    service: str,
    route: str,
    status: str,
    started_at: float,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build a JSON response with correlation id and health metrics."""
    correlation_id = correlation_id_from_request(request)
    body = dict(payload)
    body.setdefault("correlation_id", correlation_id)
    duration_ms = (time.monotonic() - started_at) * 1000
    record_control_plane_health(
        service=service,
        route=route,
        status=status,
        duration_ms=duration_ms,
    )
    response_headers = {CORRELATION_HEADER: correlation_id}
    if headers:
        response_headers.update(dict(headers))
    return JSONResponse(body, status_code=status_code, headers=response_headers)


def record_control_plane_health(
    *,
    service: str,
    route: str,
    status: str,
    duration_ms: float,
) -> None:
    """Emit lightweight OTel metrics for health/readiness endpoints."""
    counter, duration = _health_instruments()
    attributes = {
        "service.name": service,
        "http.route": route,
        "health.status": status,
    }
    counter.add(1, attributes)
    duration.record(duration_ms, attributes)


def _health_instruments() -> tuple[object, object]:
    global _HEALTH_COUNTER, _HEALTH_DURATION
    if _HEALTH_COUNTER is None or _HEALTH_DURATION is None:
        meter = get_meter()
        _HEALTH_COUNTER = meter.create_counter(
            "interlock.control_plane.health.checks",
            description="Control-plane health/readiness responses",
        )
        _HEALTH_DURATION = meter.create_histogram(
            "interlock.control_plane.health.duration_ms",
            unit="ms",
            description="Control-plane health/readiness response latency",
        )
    return _HEALTH_COUNTER, _HEALTH_DURATION
