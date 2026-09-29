"""Redaction shared by the approvals API and outbound notifications.

These helpers lived in `admin/routes/approvals.py` and were reachable only by
an authenticated reviewer over an RBAC-checked endpoint. A notification leaves
the system entirely - it reaches a Slack workspace and everyone in the channel
- so it needs at least the same treatment, and `statement_fingerprint` below is
deliberately stricter than what the API returns.

`admin/routes/approvals.py` keeps thin aliases so its own behaviour and tests
are unchanged.
"""

from __future__ import annotations

import re
from typing import Any

_SECRET_MARKERS = (
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)


def redact_sql_literals(sql_text: str) -> str:
    """Replace quoted strings, and long digit runs, with a marker."""
    redacted = re.sub(r"'(?:''|[^'])*'", "'[REDACTED]'", sql_text)
    redacted = re.sub(r"\b\d{6,}\b", "[REDACTED]", redacted)
    return redacted


def redact_metadata(value: Any) -> Any:
    """Recursively mask values whose key names suggest a credential."""
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(marker in key_text.lower().replace("-", "_") for marker in _SECRET_MARKERS):
                output[key] = "[REDACTED]"
            elif key_text == "body":
                output[key] = redact_body_metadata(item)
            else:
                output[key] = redact_metadata(item)
        return output
    if isinstance(value, list):
        return [redact_metadata(item) for item in value]
    return value


def redact_body_metadata(value: Any) -> Any:
    """Mask a recorded request body, which is arbitrary caller-supplied data."""
    if not isinstance(value, dict):
        return "[REDACTED]"
    safe = dict(value)
    data = safe.get("data")
    if isinstance(data, dict):
        safe["data"] = redact_metadata(data)
    elif data:
        safe["data"] = "[REDACTED]"
    return redact_metadata(safe)


def statement_fingerprint(sql_text: str, *, max_chars: int = 300) -> str:
    """Summarise a statement for a message that leaves the system.

    Stricter than `redact_sql_literals`, which the API uses: that keeps digit
    runs shorter than six characters, on the reasoning that a reviewer reading
    an authenticated page benefits from seeing `WHERE id = 42`. A Slack channel
    is a different audience, and a short number can still be an account or
    record identifier, so every numeric literal is replaced here.

    The result is meant to convey the shape of the statement - which table,
    which operation, roughly which shape of predicate - not to be re-executable
    or to identify a row.
    """
    redacted = redact_sql_literals(sql_text)
    redacted = re.sub(r"\b\d+(?:\.\d+)?\b", "?", redacted)
    collapsed = " ".join(redacted.split())
    if len(collapsed) > max_chars:
        return collapsed[: max_chars - 3] + "..."
    return collapsed
