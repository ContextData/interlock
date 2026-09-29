"""Security middleware for the InterLock admin API."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class AdminRateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse in-memory burst guard for Admin sign-in attempts.

    The authoritative failed-login throttle lives in the login route and
    uses Redis so it works across admin replicas. This middleware remains
    as a local burst backstop for extreme request floods. Authenticated
    dashboard navigation is intentionally not counted here; source/policy
    request limits are handled by the gateway policy rate limiter instead.
    """

    def __init__(
        self,
        app: Any,
        max_requests: int = 100,
        window_seconds: int = 60,
    ) -> None:
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # ip -> list of request timestamps
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if request.url.path != "/auth/login" or request.method.upper() != "POST":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Prune old entries
        timestamps = self._requests[client_ip]
        self._requests[client_ip] = [ts for ts in timestamps if ts > cutoff]

        if len(self._requests[client_ip]) >= self.max_requests:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
            )

        self._requests[client_ip].append(now)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security headers to all responses.

    Headers applied:
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: DENY
    - X-XSS-Protection: 1; mode=block
    - Content-Security-Policy: explicit Admin UI policy
    - Strict-Transport-Security: max-age=31536000 (when request is HTTPS)
    - Referrer-Policy: strict-origin-when-cross-origin
    - Cache-Control: no-store (for API responses)
    """

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cache-Control"] = "no-store"

        # HSTS only when the connection is HTTPS
        scheme = request.url.scheme
        if scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response
