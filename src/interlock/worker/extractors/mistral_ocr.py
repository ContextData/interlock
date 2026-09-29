"""PDF/image OCR extractor using Mistral OCR API."""

from __future__ import annotations

import base64
import logging
import os

from interlock.worker.extractors.base import ExtractedContent

logger = logging.getLogger(__name__)

_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
_MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"


def _get_api_key() -> str | None:
    """Read Mistral API key from environment."""
    return os.environ.get("MISTRAL_API_KEY")


class MistralOCRExtractor:
    """Extract text from PDFs and images via Mistral OCR API.

    Requires MISTRAL_API_KEY environment variable and httpx
    (already a project dependency).
    """

    @property
    def available(self) -> bool:
        return _get_api_key() is not None

    def can_handle(self, file_path: str, mime_type: str | None = None) -> bool:
        if _get_api_key() is None:
            return False
        ext = os.path.splitext(file_path)[1].lower()
        return ext in _EXTENSIONS

    async def extract(self, file_path: str) -> ExtractedContent:
        api_key = _get_api_key()
        if not api_key:
            return ExtractedContent(
                text="",
                metadata={"error": "MISTRAL_API_KEY not set"},
            )

        import httpx

        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }
        mime_type = mime_map.get(ext, "application/octet-stream")

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        encoded = base64.standard_b64encode(file_bytes).decode("ascii")

        payload = {
            "model": "mistral-ocr-latest",
            "document": {
                "type": "document_url",
                "document_url": f"data:{mime_type};base64,{encoded}",
            },
        }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    _MISTRAL_OCR_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Mistral OCR request failed: %s", exc)
            return ExtractedContent(
                text="",
                metadata={"error": f"OCR request failed: {exc}"},
            )

        # Parse response: pages contain markdown text
        pages = data.get("pages", [])
        parts: list[str] = []
        for page in pages:
            md = page.get("markdown", "")
            if md:
                parts.append(md)

        full_text = "\n\n".join(parts)
        return ExtractedContent(
            text=full_text,
            metadata={
                "source": file_path,
                "page_count_raw": len(pages),
                "model": data.get("model", "unknown"),
            },
            page_count=len(pages) if pages else None,
            word_count=len(full_text.split()) if full_text else 0,
        )
