"""Streaming response processor - orchestrates fast + deep PII scanning."""

from __future__ import annotations

import copy
from typing import Any

from interlock.config import PIIConfig
from interlock.models import PIIMatch
from interlock.pipeline.pii_deep import PIIDeepScanner, is_free_text_column
from interlock.pipeline.pii_fast import PIIFastScanner
from interlock.pipeline.redactor import Redactor

FieldPath = tuple[str | int, ...]


def _merge_matches(
    fast: dict[str, list[PIIMatch]],
    deep: dict[str, list[PIIMatch]],
) -> dict[str, list[PIIMatch]]:
    """Merge two field-match dicts, deduplicating by (start, end, entity_type)."""
    merged: dict[str, list[PIIMatch]] = {}

    all_fields = set(fast) | set(deep)
    for field in all_fields:
        combined = list(fast.get(field, [])) + list(deep.get(field, []))
        # Deduplicate: same span and entity type
        seen: set[tuple[int, int, str]] = set()
        unique: list[PIIMatch] = []
        for m in combined:
            key = (m.start, m.end, m.entity_type)
            if key not in seen:
                seen.add(key)
                unique.append(m)
        unique.sort(key=lambda m: m.start)
        if unique:
            merged[field] = unique

    return merged


def _iter_string_fields(value: Any, path: FieldPath = ()) -> list[tuple[FieldPath, str, str]]:
    """Return all string leaves with their path and leaf field name."""
    fields: list[tuple[FieldPath, str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            fields.extend(_iter_string_fields(item, (*path, str(key))))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            fields.extend(_iter_string_fields(item, (*path, index)))
    elif isinstance(value, str):
        leaf = next((str(part) for part in reversed(path) if isinstance(part, str)), "")
        fields.append((path, leaf, value))
    return fields


def _set_path(value: Any, path: FieldPath, replacement: Any) -> None:
    current = value
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = replacement


def _redact_scan_failure(text: str) -> tuple[str, PIIMatch]:
    match = PIIMatch(
        entity_type="PII_SCAN_FAILED",
        start=0,
        end=len(text),
        text=text,
    )
    return Redactor.redact_text(text, [match]), match


def _is_free_text_field(field_name: str, config: PIIConfig) -> bool:
    patterns = config.free_text_column_patterns or []
    if patterns:
        lowered = field_name.lower()
        return any(pattern.lower() in lowered for pattern in patterns)
    return is_free_text_column(field_name)


class ResponseProcessor:
    """Orchestrate fast and (optionally) deep PII scanning with redaction."""

    def __init__(
        self,
        fast_scanner: PIIFastScanner,
        deep_scanner: PIIDeepScanner | None = None,
        config: PIIConfig | None = None,
    ) -> None:
        self._fast = fast_scanner
        self._deep = deep_scanner
        self._config = config or PIIConfig()

    async def process_row(
        self,
        row: dict[str, Any],
        column_names: list[str] | None = None,
    ) -> tuple[dict[str, Any], list[PIIMatch]]:
        """Process a single row through the PII pipeline.

        Steps:
          1. Fast-scan all string fields
          2. Deep-scan free-text fields > 50 chars (if deep scanner available)
          3. Merge and deduplicate matches
          4. Redact matched fields

        Returns ``(redacted_row, all_matches)``.
        """
        output = copy.deepcopy(row)
        string_fields = _iter_string_fields(row)

        fast_matches: dict[FieldPath, list[PIIMatch]] = {}
        if self._config.fast_enabled:
            try:
                for path, _field_name, text in string_fields:
                    matches = self._fast.scan(text)
                    if matches:
                        fast_matches[path] = matches
            except Exception:
                all_matches: list[PIIMatch] = []
                for path, _field_name, text in string_fields:
                    redacted, match = _redact_scan_failure(text)
                    _set_path(output, path, redacted)
                    all_matches.append(match)
                return output, all_matches

        deep_matches: dict[FieldPath, list[PIIMatch]] = {}
        if self._deep is not None and self._deep.available and self._config.deep_enabled:
            for path, field_name, text in string_fields:
                if not _is_free_text_field(field_name, self._config):
                    continue
                try:
                    matches = await self._deep.scan(text)
                except Exception:
                    _redacted, match = _redact_scan_failure(text)
                    deep_matches[path] = [match]
                    break
                if matches:
                    deep_matches[path] = matches

        all_paths = set(fast_matches) | set(deep_matches)
        all_matches: list[PIIMatch] = []
        for path in all_paths:
            merged = _merge_matches(
                {"value": fast_matches.get(path, [])},
                {"value": deep_matches.get(path, [])},
            ).get("value", [])
            if not merged:
                continue
            text = next(
                text for field_path, _field_name, text in string_fields if field_path == path
            )
            _set_path(output, path, Redactor.redact_text(text, merged))
            all_matches.extend(merged)

        all_matches.sort(key=lambda match: match.start)
        return output, all_matches

    async def process_rows(
        self,
        rows: list[dict[str, Any]],
        column_names: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Process multiple rows.

        Returns ``(redacted_rows, pii_detected)``.
        """
        redacted_rows: list[dict[str, Any]] = []
        pii_detected = False

        for row in rows:
            redacted, matches = await self.process_row(row, column_names)
            redacted_rows.append(redacted)
            if matches:
                pii_detected = True

        return redacted_rows, pii_detected
