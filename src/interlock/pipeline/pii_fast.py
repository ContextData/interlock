"""Regex-based PII detection - fast tier that runs in-process."""

from __future__ import annotations

import re
from typing import Any

from interlock.models import PIIMatch

# Pattern definitions: (entity_type, regex_pattern)
_PATTERN_DEFS: list[tuple[str, str]] = [
    ("SSN", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("CREDIT_CARD", r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    ("EMAIL", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    ("PHONE", r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    ("IP_ADDRESS", r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"),
    ("MRN", r"\bMRN[-:\s]?\d{6,10}\b"),
]


class PIIFastScanner:
    """Fast regex-based PII scanner.

    Compiles patterns once at init time and reuses them for every scan.
    """

    def __init__(self) -> None:
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        """Compile all regex patterns."""
        for entity_type, pattern in _PATTERN_DEFS:
            self._patterns.append((entity_type, re.compile(pattern)))

    def scan(self, text: str) -> list[PIIMatch]:
        """Scan text for PII using compiled regex patterns.

        Returns a list of PIIMatch objects sorted by start position.
        """
        if not text:
            return []

        matches: list[PIIMatch] = []
        for entity_type, pattern in self._patterns:
            for m in pattern.finditer(text):
                matches.append(
                    PIIMatch(
                        entity_type=entity_type,
                        start=m.start(),
                        end=m.end(),
                        text=m.group(),
                    )
                )

        # Sort by start position for deterministic output
        matches.sort(key=lambda x: x.start)
        return matches

    def scan_row(self, row: dict[str, Any]) -> dict[str, list[PIIMatch]]:
        """Scan all string fields in a row dict.

        Returns a mapping of field_name to list of PIIMatch objects.
        Only fields with matches are included in the result.
        """
        results: dict[str, list[PIIMatch]] = {}
        for field_name, value in row.items():
            if isinstance(value, str):
                matches = self.scan(value)
                if matches:
                    results[field_name] = matches
        return results

    def redact(self, text: str, matches: list[PIIMatch] | None = None) -> str:
        """Replace PII matches with [REDACTED:{entity_type}] placeholders.

        If matches is None, a fresh scan is performed first.
        Processes matches in reverse order to preserve character positions.
        """
        if not text:
            return text

        if matches is None:
            matches = self.scan(text)

        if not matches:
            return text

        # Sort by start descending so replacements don't shift positions
        sorted_matches = sorted(matches, key=lambda x: x.start, reverse=True)

        result = text
        for match in sorted_matches:
            placeholder = f"[REDACTED:{match.entity_type}]"
            result = result[: match.start] + placeholder + result[match.end :]

        return result
