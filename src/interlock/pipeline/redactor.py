"""Text redaction utilities for PII matches."""

from __future__ import annotations

from typing import Any

from interlock.models import PIIMatch


class Redactor:
    """Replace PII matches in text with redaction markers."""

    @staticmethod
    def redact_text(
        text: str,
        matches: list[PIIMatch],
        replacement_template: str = "[REDACTED:{entity_type}]",
    ) -> str:
        """Replace PII matches in *text* with redaction placeholders.

        Matches are processed in reverse-start order so that earlier character
        positions remain valid after each replacement.
        """
        if not text or not matches:
            return text

        sorted_matches = sorted(matches, key=lambda m: m.start, reverse=True)
        result = text
        for match in sorted_matches:
            placeholder = replacement_template.format(entity_type=match.entity_type)
            result = result[: match.start] + placeholder + result[match.end :]
        return result

    @staticmethod
    def redact_row(
        row: dict[str, Any],
        field_matches: dict[str, list[PIIMatch]],
    ) -> dict[str, Any]:
        """Return a copy of *row* with all matched fields redacted."""
        if not field_matches:
            return dict(row)

        redacted = dict(row)
        for field_name, matches in field_matches.items():
            if field_name in redacted and isinstance(redacted[field_name], str):
                redacted[field_name] = Redactor.redact_text(redacted[field_name], matches)
        return redacted
