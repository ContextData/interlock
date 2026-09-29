"""Turning a failed scan into something safe to store and show.

A scan's error is displayed in the console and kept in the scan history, so it
must never carry a DSN, a password or a token. Exceptions are reduced to a
stable `error_code` plus a scrubbed message; the full traceback goes to the log
only.
"""

from __future__ import annotations

import asyncio
import re
import ssl

from interlock.errors import EgressBlockedError

_DSN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"]+", re.IGNORECASE)
_KEY_VALUE_SECRET = re.compile(
    r"\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
    r"\s*[=:]\s*\S+",
    re.IGNORECASE,
)
_MAX_MESSAGE = 300


class CatalogScanError(Exception):
    """A scan failed for a reason worth recording with a specific code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def scrub(message: str) -> str:
    """Remove anything that looks like a connection string or a credential."""
    cleaned = _DSN.sub("<redacted-url>", message)
    cleaned = _KEY_VALUE_SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_MESSAGE:
        cleaned = cleaned[: _MAX_MESSAGE - 3] + "..."
    return cleaned


def classify_error(exc: BaseException) -> tuple[str, str]:
    """Return a stable error code and a scrubbed, bounded message."""
    if isinstance(exc, CatalogScanError):
        return exc.code, scrub(exc.message)
    if isinstance(exc, EgressBlockedError):
        # The source's host is private or reserved and the source was not
        # configured to allow that; the fix is in the source's configuration.
        return "egress_blocked", scrub(str(exc))
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout", "the scan exceeded its time limit"
    if isinstance(exc, ssl.SSLError):
        return "tls", scrub(f"TLS failure: {exc}")
    if isinstance(exc, (ConnectionError, OSError)):
        return "network", scrub(f"could not reach the source: {exc}")

    name = type(exc).__name__
    lowered = f"{name} {exc}".lower()
    if "tls" in lowered or "ssl" in lowered or "verified upstream" in lowered:
        return "tls_refused", scrub(str(exc))
    if "password" in lowered or "authentication" in lowered or "auth" in name.lower():
        return "auth", "the source rejected the configured credentials"
    if "permission" in lowered or "privilege" in lowered or "denied" in lowered:
        return "permission", scrub(str(exc))
    return "internal", scrub(f"{name}: {exc}")
