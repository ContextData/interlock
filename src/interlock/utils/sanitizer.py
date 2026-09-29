"""Input sanitization utilities to prevent injection attacks."""

from __future__ import annotations

import os
import re

# SQL identifiers: alphanumeric, underscore, dot (schema.table)
_SQL_IDENT_RE = re.compile(r"[^A-Za-z0-9_.]")

# Source IDs: alphanumeric, underscore, hyphen
_SOURCE_ID_RE = re.compile(r"[^A-Za-z0-9_\-]")


class RequestSanitizer:
    """Sanitizes inputs to prevent injection attacks."""

    @staticmethod
    def sanitize_sql_identifier(identifier: str) -> str:
        """Sanitize a SQL identifier (table/column name).

        Allows only alphanumeric characters, underscores, and dots
        (for schema.table notation). All other characters are stripped.

        Raises ValueError if the result is empty.
        """
        cleaned = _SQL_IDENT_RE.sub("", identifier)
        if not cleaned:
            raise ValueError(f"SQL identifier is empty after sanitization: {identifier!r}")
        # Prevent leading dots or consecutive dots
        cleaned = re.sub(r"\.{2,}", ".", cleaned).strip(".")
        if not cleaned:
            raise ValueError(f"SQL identifier is empty after sanitization: {identifier!r}")
        return cleaned

    @staticmethod
    def sanitize_path(path: str) -> str:
        """Sanitize a file path to prevent path traversal.

        Resolves the path and ensures it does not contain '..' components
        that could escape the intended directory.

        Returns the normalized absolute path.
        Raises ValueError if traversal is detected.
        """
        # Reject null bytes
        if "\x00" in path:
            raise ValueError("Path contains null byte")

        # Normalize and resolve
        normalized = os.path.normpath(path)

        # Check for traversal patterns in the original input
        if ".." in path.split(os.sep) or ".." in path.split("/"):
            raise ValueError(f"Path traversal detected: {path!r}")

        return normalized

    @staticmethod
    def sanitize_source_id(source_id: str) -> str:
        """Sanitize a source identifier.

        Allows only alphanumeric characters, underscores, and hyphens.
        Raises ValueError if the result is empty.
        """
        cleaned = _SOURCE_ID_RE.sub("", source_id)
        if not cleaned:
            raise ValueError(f"Source ID is empty after sanitization: {source_id!r}")
        return cleaned

    @staticmethod
    def validate_api_key_format(api_key: str) -> bool:
        """Validate that an API key meets minimum security requirements.

        Requirements:
        - At least 32 characters long
        - Contains only printable ASCII
        - Contains no whitespace, quotes, angle brackets, or backticks
        - Not all the same character
        """
        if len(api_key) < 32:
            return False

        stripped = api_key.strip()
        if len(stripped) != len(api_key):
            return False  # Leading/trailing whitespace

        # Must be printable ASCII (32-126)
        if not all(32 <= ord(c) <= 126 for c in api_key):
            return False

        if any(c.isspace() for c in api_key):
            return False

        if any(c in api_key for c in ("'", '"', "<", ">", "`")):
            return False

        # Not all the same character
        if len(set(api_key)) == 1:
            return False

        return True

    @staticmethod
    def mask_sensitive_value(value: str, visible_chars: int = 4) -> str:
        """Mask a sensitive value for logging, showing only the last N chars.

        Example: "sk-abc123xyz789" -> "***********789" (visible_chars=3)
        """
        if not value:
            return "***"

        if len(value) <= visible_chars:
            return "*" * len(value)

        masked_len = len(value) - visible_chars
        return "*" * masked_len + value[-visible_chars:]
