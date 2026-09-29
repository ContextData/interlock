"""Tests for the LLM document summarizer."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from interlock.worker.summarizer import DocumentSummarizer, DocumentSummary


def _make_response(status_code: int, json_data: dict) -> httpx.Response:
    """Create an httpx.Response with a request set so raise_for_status works."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status_code, json=json_data, request=request)


class TestDocumentSummaryModel:
    def test_defaults(self) -> None:
        s = DocumentSummary()
        assert s.title == ""
        assert s.doc_type == ""
        assert s.summary == ""
        assert s.topics == []
        assert s.entities == []
        assert s.quality_signals == {}

    def test_full_construction(self) -> None:
        s = DocumentSummary(
            title="Test Doc",
            doc_type="report",
            summary="A test document.",
            topics=["testing", "docs"],
            entities=["Acme Corp"],
            category_path="engineering.testing",
            time_relevance="current",
            audience="engineers",
            quality_signals={"completeness": 0.9},
        )
        assert s.title == "Test Doc"
        assert s.topics == ["testing", "docs"]
        assert s.quality_signals["completeness"] == 0.9


class TestDocumentSummarizerAvailability:
    def test_not_available_without_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            s = DocumentSummarizer(api_key="")
            assert not s.available

    def test_available_with_key(self) -> None:
        s = DocumentSummarizer(api_key="sk-test-123")
        assert s.available


class TestFallbackSummary:
    @pytest.mark.asyncio
    async def test_fallback_when_not_available(self) -> None:
        s = DocumentSummarizer(api_key="")
        result = await s.summarize(
            content="This is a test document with some content.",
            file_path="/data/my_report.pdf",
        )
        assert isinstance(result, DocumentSummary)
        assert result.title == "My Report"
        assert result.doc_type == "report"
        assert "test document" in result.summary

    @pytest.mark.asyncio
    async def test_fallback_title_from_filename(self) -> None:
        s = DocumentSummarizer(api_key="")
        result = await s.summarize(
            content="Some content here.",
            file_path="/docs/project_plan.docx",
        )
        assert result.title == "Project Plan"

    @pytest.mark.asyncio
    async def test_fallback_doc_type_from_extension(self) -> None:
        s = DocumentSummarizer(api_key="")

        py_result = await s.summarize("x = 1", file_path="main.py")
        assert py_result.doc_type == "code"

        csv_result = await s.summarize("a,b,c", file_path="data.csv")
        assert csv_result.doc_type == "spreadsheet"

        unknown_result = await s.summarize("stuff", file_path="file.xyz")
        assert unknown_result.doc_type == "other"

    @pytest.mark.asyncio
    async def test_fallback_extracts_topics(self) -> None:
        s = DocumentSummarizer(api_key="")
        result = await s.summarize(
            content="Amazon Web Services and Google Cloud Platform are popular.",
            file_path="report.txt",
        )
        # Should extract capitalized phrases
        assert len(result.topics) > 0

    @pytest.mark.asyncio
    async def test_fallback_empty_content(self) -> None:
        s = DocumentSummarizer(api_key="")
        result = await s.summarize(content="", file_path="empty.txt")
        assert result.summary == ""
        assert result.topics == []


class TestSummarizeWithMockedAPI:
    @pytest.mark.asyncio
    async def test_successful_api_call(self) -> None:
        good_response = {
            "content": [
                {
                    "text": json.dumps(
                        {
                            "title": "API Design Guide",
                            "doc_type": "documentation",
                            "summary": "A guide to designing REST APIs.",
                            "topics": ["REST", "API"],
                            "entities": [],
                            "category_path": "engineering.api",
                            "time_relevance": "evergreen",
                            "audience": "engineers",
                            "quality_signals": {"completeness": 0.8},
                        }
                    )
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        mock_response = _make_response(200, good_response)
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            s = DocumentSummarizer(api_key="sk-test")
            result = await s.summarize("Some API docs content.", file_path="api.md")

        assert result.title == "API Design Guide"
        assert result.doc_type == "documentation"
        assert "REST" in result.topics

    @pytest.mark.asyncio
    async def test_retry_on_bad_json(self) -> None:
        """First call returns bad JSON, second returns good JSON."""
        bad_response = _make_response(
            200,
            {"content": [{"text": "not valid json {{{"}], "usage": {}},
        )
        good_json = json.dumps(
            {
                "title": "Recovered",
                "doc_type": "report",
                "summary": "Recovered on retry.",
                "topics": [],
                "entities": [],
                "category_path": "",
                "time_relevance": "current",
                "audience": "everyone",
                "quality_signals": {},
            }
        )
        good_response = _make_response(
            200,
            {"content": [{"text": good_json}], "usage": {}},
        )

        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=[bad_response, good_response],
        ):
            s = DocumentSummarizer(api_key="sk-test")
            result = await s.summarize("content", file_path="doc.pdf")

        assert result.title == "Recovered"

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_returns_fallback(self) -> None:
        """If all retries return bad JSON, fall back."""
        bad_response = _make_response(
            200,
            {"content": [{"text": "garbage"}], "usage": {}},
        )

        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            return_value=bad_response,
        ):
            s = DocumentSummarizer(api_key="sk-test")
            result = await s.summarize("Some content.", file_path="notes.txt")

        # Should be a fallback summary
        assert result.title == "Notes"
        assert result.doc_type == "documentation"

    @pytest.mark.asyncio
    async def test_api_timeout_returns_fallback(self) -> None:
        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timeout"),
        ):
            s = DocumentSummarizer(api_key="sk-test")
            result = await s.summarize("content", file_path="report.pdf")

        assert isinstance(result, DocumentSummary)
        assert result.title == "Report"

    @pytest.mark.asyncio
    async def test_markdown_fence_stripped(self) -> None:
        """API response wrapped in markdown code fences should still parse."""
        fenced = (
            "```json\n"
            + json.dumps(
                {
                    "title": "Fenced",
                    "doc_type": "code",
                    "summary": "test",
                    "topics": [],
                    "entities": [],
                    "category_path": "",
                    "time_relevance": "",
                    "audience": "",
                    "quality_signals": {},
                }
            )
            + "\n```"
        )
        response = _make_response(
            200,
            {"content": [{"text": fenced}], "usage": {}},
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response):
            s = DocumentSummarizer(api_key="sk-test")
            result = await s.summarize("code here", file_path="main.py")

        assert result.title == "Fenced"
