"""Excel extractor - requires openpyxl."""

from __future__ import annotations

import io
import os

from interlock.worker.extractors.base import ExtractedContent

try:
    import openpyxl

    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False

_EXTENSIONS = {".xlsx", ".xls"}
_MAX_SAMPLE_ROWS = 20


class XlsxExtractor:
    """Extract content from Excel files using openpyxl."""

    @property
    def available(self) -> bool:
        return _HAS_OPENPYXL

    def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
        if not _HAS_OPENPYXL:
            return False
        ext = os.path.splitext(file_path)[1].lower()
        return ext in _EXTENSIONS

    async def extract(self, file_path: str) -> ExtractedContent:
        if not _HAS_OPENPYXL:
            return ExtractedContent(
                text="",
                metadata={"error": "openpyxl not installed"},
            )

        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        buf = io.StringIO()
        sheet_names = wb.sheetnames

        for sheet_name in sheet_names:
            ws = wb[sheet_name]
            buf.write(f"--- Sheet: {sheet_name} ---\n")

            headers: list[str] = []
            row_count = 0

            for i, row in enumerate(ws.iter_rows(values_only=True)):
                str_cells = [str(c) if c is not None else "" for c in row]
                if i == 0:
                    headers = str_cells
                    buf.write("Columns: " + ", ".join(headers) + "\n")
                else:
                    row_count += 1
                    if row_count <= _MAX_SAMPLE_ROWS:
                        buf.write("\t".join(str_cells) + "\n")

            buf.write(f"Total rows: {row_count}\n\n")

        wb.close()
        text = buf.getvalue()

        return ExtractedContent(
            text=text,
            metadata={
                "source": file_path,
                "sheet_names": sheet_names,
                "sheet_count": len(sheet_names),
            },
            word_count=len(text.split()),
        )
