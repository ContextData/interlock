"""What a cached answer's redaction was, carried with the entry.

A cached response is stored after redaction, so serving it again masks the
same values. The audit row for that hit used to record `pii_detected=False`
because no scanner ran on this invocation, which read as "nothing was
masked" while the agent was looking at `[REDACTED:...]` tokens. Each protocol
now stores the redaction result with the entry and copies it into the audit
row on a hit.
"""

from __future__ import annotations

from typing import Any

_KEY = "redaction"


def with_redaction(
    metadata: dict[str, Any],
    *,
    pii_detected: bool,
    pii_types: list[str] | None,
    redaction_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`metadata` plus the redaction applied to the response being cached."""
    return {
        **metadata,
        _KEY: {
            "pii_detected": bool(pii_detected),
            "pii_types": sorted(set(pii_types or [])),
            "redaction_stats": dict(redaction_stats or {}),
        },
    }


def redaction_of(metadata: dict[str, Any] | None) -> tuple[bool, list[str], dict[str, Any]]:
    """The redaction recorded with a cached entry: (pii_detected, pii_types, stats).

    An entry written before this existed carries nothing, and reads as
    no redaction recorded, which is what the audit said before.
    """
    recorded = (metadata or {}).get(_KEY) or {}
    return (
        bool(recorded.get("pii_detected")),
        list(recorded.get("pii_types") or []),
        dict(recorded.get("redaction_stats") or {}),
    )
