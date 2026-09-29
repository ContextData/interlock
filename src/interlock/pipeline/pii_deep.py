"""Deep PII detection using Presidio (optional dependency).

If presidio-analyzer or spaCy are not installed, the scanner gracefully
degrades: initialize() logs a warning and all scan methods return empty results.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from typing import Any

from interlock.models import PIIMatch

logger = logging.getLogger(__name__)


class PIIDeepScanError(RuntimeError):
    """Raised when an available deep scanner fails during analysis."""


# ---------------------------------------------------------------------------
# Free-text column heuristic
# ---------------------------------------------------------------------------

FREE_TEXT_PATTERNS = [
    "description",
    "notes",
    "comment",
    "bio",
    "message",
    "body",
    "content",
    "remarks",
    "summary",
    "text",
    "detail",
    "narrative",
    "memo",
    "review",
    "feedback",
]


def is_free_text_column(column_name: str) -> bool:
    """Check if a column name suggests free-text content."""
    name_lower = column_name.lower()
    return any(pattern in name_lower for pattern in FREE_TEXT_PATTERNS)


# ---------------------------------------------------------------------------
# Worker function (runs in subprocess)
# ---------------------------------------------------------------------------


def _presidio_scan_worker(text: str) -> list[dict[str, Any]]:
    """Run Presidio analysis in a worker process.

    Returns a list of dicts so the result can be pickled across processes.
    """
    # Lazy imports inside the worker to keep the main process light
    from presidio_analyzer import AnalyzerEngine  # type: ignore[import-untyped]

    analyzer = AnalyzerEngine()
    results = analyzer.analyze(text=text, language="en")
    return [
        {
            "entity_type": r.entity_type,
            "start": r.start,
            "end": r.end,
            "text": text[r.start : r.end],
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# PIIDeepScanner
# ---------------------------------------------------------------------------


class PIIDeepScanner:
    """Deep PII scanner backed by Presidio and spaCy.

    Falls back to a no-op when the optional dependencies are missing.
    """

    # Minimum text length worth sending through Presidio
    MIN_TEXT_LENGTH = 50

    def __init__(self, max_workers: int = 4) -> None:
        self._max_workers = max_workers
        self._executor: ProcessPoolExecutor | None = None
        self._available = False

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Try to create a ProcessPoolExecutor and verify Presidio is importable.

        On failure (missing deps), sets ``_available = False`` and logs a warning.
        """
        try:
            import presidio_analyzer  # noqa: F401
            import spacy  # noqa: F401

            self._executor = ProcessPoolExecutor(max_workers=self._max_workers)
            self._available = True
            logger.info("PIIDeepScanner initialized with %d workers", self._max_workers)
        except ImportError:
            self._available = False
            logger.warning("Presidio/spaCy not installed - deep PII scanning disabled")

    async def shutdown(self) -> None:
        """Shutdown the process pool executor."""
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None
        self._available = False

    # -- scanning ------------------------------------------------------------

    async def scan(self, text: str) -> list[PIIMatch]:
        """Scan *text* for PII using Presidio.

        Returns an empty list when the scanner is unavailable or the text is
        shorter than ``MIN_TEXT_LENGTH``.
        """
        if not self._available or self._executor is None:
            return []

        if len(text) <= self.MIN_TEXT_LENGTH:
            return []

        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(self._executor, partial(_presidio_scan_worker, text))
        except Exception as exc:
            logger.exception("Presidio scan failed")
            raise PIIDeepScanError("deep PII scan failed") from exc

        return [
            PIIMatch(
                entity_type=r["entity_type"],
                start=r["start"],
                end=r["end"],
                text=r["text"],
            )
            for r in raw
        ]

    async def scan_fields(self, fields: dict[str, str]) -> dict[str, list[PIIMatch]]:
        """Scan multiple text fields, filtering to free-text columns only.

        Returns a mapping of field_name -> matches (only fields with matches).
        """
        results: dict[str, list[PIIMatch]] = {}

        for name, value in fields.items():
            if not is_free_text_column(name):
                continue
            if not isinstance(value, str):
                continue
            matches = await self.scan(value)
            if matches:
                results[name] = matches

        return results

    # -- properties ----------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available
