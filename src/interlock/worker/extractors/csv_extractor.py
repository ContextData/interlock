"""CSV/TSV extractor - uses pandas if available, falls back to stdlib csv."""

from __future__ import annotations

import csv
import io
import os

from interlock.worker.extractors.base import ExtractedContent

try:
    import pandas as pd

    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

_EXTENSIONS = {".csv": ",", ".tsv": "\t"}
_MAX_SAMPLE_ROWS = 20


class CSVExtractor:
    """Extract content from CSV/TSV files.

    Uses pandas for richer extraction when available, otherwise
    falls back to the csv stdlib module.
    """

    def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        return ext in _EXTENSIONS

    async def extract(self, file_path: str) -> ExtractedContent:
        ext = os.path.splitext(file_path)[1].lower()
        delimiter = _EXTENSIONS.get(ext, ",")

        if _HAS_PANDAS:
            return self._extract_pandas(file_path, delimiter)
        return self._extract_stdlib(file_path, delimiter)

    def _extract_pandas(self, file_path: str, delimiter: str) -> ExtractedContent:
        df = pd.read_csv(file_path, delimiter=delimiter, nrows=_MAX_SAMPLE_ROWS)
        total_rows = sum(1 for _ in open(file_path, encoding="utf-8")) - 1

        buf = io.StringIO()
        buf.write(f"Columns: {', '.join(df.columns)}\n")
        buf.write(f"Total rows: {total_rows}\n\n")
        buf.write(df.to_string(index=False))
        text = buf.getvalue()

        return ExtractedContent(
            text=text,
            metadata={
                "source": file_path,
                "columns": list(df.columns),
                "total_rows": total_rows,
                "backend": "pandas",
            },
            word_count=len(text.split()),
        )

    def _extract_stdlib(self, file_path: str, delimiter: str) -> ExtractedContent:
        rows: list[list[str]] = []
        total_rows = 0
        headers: list[str] = []

        with open(file_path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=delimiter)
            for i, row in enumerate(reader):
                if i == 0:
                    headers = row
                else:
                    total_rows += 1
                    if len(rows) < _MAX_SAMPLE_ROWS:
                        rows.append(row)

        buf = io.StringIO()
        buf.write(f"Columns: {', '.join(headers)}\n")
        buf.write(f"Total rows: {total_rows}\n\n")
        for row in rows:
            buf.write("\t".join(row) + "\n")
        text = buf.getvalue()

        return ExtractedContent(
            text=text,
            metadata={
                "source": file_path,
                "columns": headers,
                "total_rows": total_rows,
                "backend": "csv_stdlib",
            },
            word_count=len(text.split()),
        )
