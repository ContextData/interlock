"""LLM-powered document summarization using Anthropic Haiku."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM_PROMPT = """\
You are a document analysis assistant. Given document content (possibly sampled), \
produce a structured JSON summary. Respond with ONLY a valid JSON object matching \
this schema - no markdown, no explanation:

{
  "title": "descriptive title",
  "doc_type": "analytical_report|executive_summary|dataset|presentation|policy_document|technical_spec|meeting_notes|correspondence|template|reference_material|code_documentation|runbook|design_document|research_paper|financial_report|legal_document|marketing_material|training_material|other",
  "summary": "3-5 sentence summary of what the document contains and what questions it can answer",
  "topics": ["8-15 specific topics"],
  "entities": {
    "people": [],
    "organizations": [],
    "products": [],
    "systems": [],
    "metrics": [],
    "projects": [],
    "locations": [],
    "dates_periods": []
  },
  "category_path": "domain > subdomain > area",
  "time_relevance": "specific period|ongoing|historical",
  "audience": "executive|technical|operations|legal|marketing|general",
  "data_types": ["metrics", "narratives", "charts", "tables", "code", "procedures", "configurations", "formulas"],
  "quality_signals": {"completeness": "complete|partial|draft", "recency": "current|recent|outdated", "depth": "deep_analysis|overview|reference"},
  "confidence": 0.0,
  "cost_metadata": {}
}
"""

_USER_TEMPLATE = """\
Analyze this document and return a JSON summary.

File: {file_path}
{metadata_section}
Content:
{content}

Return ONLY valid JSON matching the schema above."""

_RETRY_USER_TEMPLATE = """\
Your previous response was not valid JSON. Please try again.

Analyze this document and return ONLY a valid JSON object (no markdown fences, \
no explanation text). The JSON must have these exact keys: title, doc_type, summary, \
topics, entities, category_path, time_relevance, audience, quality_signals.

File: {file_path}
Content (first 1000 words):
{content}

Return ONLY the JSON object."""

# Extension to doc_type mapping for fallback
_EXT_DOC_TYPE: dict[str, str] = {
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".go": "code",
    ".rs": "code",
    ".java": "code",
    ".rb": "code",
    ".c": "code",
    ".cpp": "code",
    ".h": "code",
    ".md": "documentation",
    ".rst": "documentation",
    ".txt": "documentation",
    ".html": "documentation",
    ".csv": "spreadsheet",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".pdf": "report",
    ".docx": "report",
    ".doc": "report",
    ".pptx": "presentation",
    ".eml": "email",
    ".msg": "email",
}


class DocumentSummary(BaseModel):
    """Structured summary of a document."""

    title: str = ""
    doc_type: str = ""
    summary: str = ""
    topics: list[str] = Field(default_factory=list)
    entities: dict[str, list[str]] | list[str] = Field(default_factory=list)
    category_path: str = ""
    time_relevance: str = ""
    audience: str = ""
    data_types: list[str] = Field(default_factory=list)
    quality_signals: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None
    cost_metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentSummarizer:
    """Generates structured document summaries using Anthropic Haiku."""

    MAX_RETRIES = 2

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-3-haiku-20240307",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model
        self._timeout = timeout_seconds
        self._available = bool(self._api_key)

    @property
    def available(self) -> bool:
        return self._available

    async def summarize(
        self,
        content: str,
        file_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> DocumentSummary:
        """Generate a structured summary of document content.

        If the API is not available (no key), returns a fallback summary
        derived directly from the content.
        """
        if not self._available:
            return self._fallback_summary(content, file_path, metadata)

        try:
            return await self._call_api(content, file_path, metadata)
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            logger.warning("Summarizer API error: %s", exc)
            return self._fallback_summary(content, file_path, metadata)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Summarizer unexpected error: %s", exc)
            return self._fallback_summary(content, file_path, metadata)

    # -- API call with retry ---------------------------------------------------

    async def _call_api(
        self,
        content: str,
        file_path: str,
        metadata: dict[str, Any] | None,
    ) -> DocumentSummary:
        """Call Anthropic Messages API, retrying on bad JSON up to MAX_RETRIES."""
        metadata_section = ""
        if metadata:
            metadata_section = f"Metadata: {json.dumps(metadata, default=str)}"

        # Truncate content to avoid huge payloads (~8000 words)
        words = content.split()
        truncated = " ".join(words[:8000]) if len(words) > 8000 else content

        user_msg = _USER_TEMPLATE.format(
            file_path=file_path,
            metadata_section=metadata_section,
            content=truncated,
        )

        for attempt in range(1 + self.MAX_RETRIES):
            if attempt > 0:
                # Use a more explicit retry prompt with shorter content
                short_content = " ".join(words[:1000])
                user_msg = _RETRY_USER_TEMPLATE.format(
                    file_path=file_path,
                    content=short_content,
                )

            text = await self._send_request(user_msg)
            result = self._parse_response(text)
            if result is not None:
                return result

            logger.debug(
                "Bad JSON on attempt %d/%d: %.100s",
                attempt + 1,
                1 + self.MAX_RETRIES,
                text,
            )

        # All retries exhausted - fall back
        logger.warning("All %d summarization attempts returned bad JSON", 1 + self.MAX_RETRIES)
        return self._fallback_summary(content, file_path, metadata)

    async def _send_request(self, user_message: str) -> str:
        """Send a single request to Anthropic API and return text response."""
        timeout = httpx.Timeout(self._timeout, connect=5.0)
        payload = {
            "model": self._model,
            "max_tokens": 1024,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _ANTHROPIC_API_URL,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()

        body = resp.json()
        content_blocks = body.get("content", [])
        if not content_blocks:
            return ""
        text = content_blocks[0].get("text", "")
        return text if isinstance(text, str) else ""

    # -- parsing ---------------------------------------------------------------

    @staticmethod
    def _parse_response(text: str) -> DocumentSummary | None:
        """Parse JSON from LLM response, returning None on failure."""
        # Strip markdown code fences if present
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            data = json.loads(cleaned)
            return DocumentSummary(**data)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    # -- fallback --------------------------------------------------------------

    @staticmethod
    def _fallback_summary(
        content: str,
        file_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> DocumentSummary:
        """Generate a minimal summary without LLM access."""
        # Title from filename
        title = ""
        if file_path:
            p = Path(file_path)
            title = p.stem.replace("_", " ").replace("-", " ").title()

        # Doc type from extension
        doc_type = "other"
        if file_path:
            ext = Path(file_path).suffix.lower()
            doc_type = _EXT_DOC_TYPE.get(ext, "other")

        # Summary from first 200 words
        words = content.split()
        summary = " ".join(words[:200]) if words else ""

        # Topics: extract simple keywords from first paragraph
        topics: list[str] = []
        if content.strip():
            first_para = content.strip().split("\n\n")[0] if "\n\n" in content else content
            # Extract capitalized multi-word phrases or single significant words
            candidates = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", first_para)
            seen: set[str] = set()
            for c in candidates:
                lower = c.lower()
                if lower not in seen and len(c) > 3:
                    seen.add(lower)
                    topics.append(c)
                if len(topics) >= 5:
                    break

        return DocumentSummary(
            title=title,
            doc_type=doc_type,
            summary=summary,
            topics=topics,
        )
