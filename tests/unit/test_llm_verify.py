"""Unit tests for the LLM verification gate."""

from __future__ import annotations

import json

import httpx
import respx

from interlock.cache.llm_verify import _ANTHROPIC_API_URL, LLMVerifier
from interlock.models import LLMVerifyResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_response(
    equivalent: bool,
    confidence: float,
    reason: str,
    input_tokens: int = 50,
    output_tokens: int = 30,
) -> dict:
    """Build a mock Anthropic Messages API response body."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"equivalent": equivalent, "confidence": confidence, "reason": reason}
                ),
            }
        ],
        "model": "claude-3-haiku-20240307",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLLMVerifier:
    """Tests for LLMVerifier."""

    def test_not_available_without_api_key(self) -> None:
        verifier = LLMVerifier(api_key="")
        assert verifier.available is False

    def test_available_with_api_key(self) -> None:
        verifier = LLMVerifier(api_key="sk-test-key")
        assert verifier.available is True

    async def test_verify_returns_false_when_not_available(self) -> None:
        verifier = LLMVerifier(api_key="")
        result = await verifier.verify("select users", "select users")
        assert result.equivalent is False
        assert result.reason == "no_api_key"

    @respx.mock
    async def test_verify_success_equivalent(self) -> None:
        respx.post(_ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                json=_api_response(True, 0.95, "same query intent"),
            )
        )
        verifier = LLMVerifier(api_key="sk-test")
        result = await verifier.verify("get all active users", "fetch active users")

        assert result.equivalent is True
        assert result.confidence == 0.95
        assert result.reason == "same query intent"

    @respx.mock
    async def test_verify_success_not_equivalent(self) -> None:
        respx.post(_ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                json=_api_response(False, 0.1, "different tables"),
            )
        )
        verifier = LLMVerifier(api_key="sk-test")
        result = await verifier.verify("get users", "get orders")

        assert result.equivalent is False
        assert result.confidence == 0.1

    @respx.mock
    async def test_verify_timeout_returns_false(self) -> None:
        respx.post(_ANTHROPIC_API_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
        verifier = LLMVerifier(api_key="sk-test", timeout_ms=100)
        result = await verifier.verify("query a", "query b")

        assert result.equivalent is False
        assert "TimeoutException" in result.reason or "ReadTimeout" in result.reason

    @respx.mock
    async def test_verify_api_error_returns_false(self) -> None:
        respx.post(_ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(500, json={"error": "internal"})
        )
        verifier = LLMVerifier(api_key="sk-test")
        result = await verifier.verify("query a", "query b")

        assert result.equivalent is False
        assert "api_error" in result.reason

    @respx.mock
    async def test_verify_parse_error_returns_false(self) -> None:
        """LLM returns non-JSON text."""
        respx.post(_ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "not valid json!!!"}],
                    "model": "claude-3-haiku-20240307",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )
        )
        verifier = LLMVerifier(api_key="sk-test")
        result = await verifier.verify("query a", "query b")

        assert result.equivalent is False
        assert "parse_error" in result.reason

    @respx.mock
    async def test_cost_tracking_increments(self) -> None:
        respx.post(_ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                json=_api_response(True, 0.9, "same", input_tokens=100, output_tokens=50),
            )
        )
        verifier = LLMVerifier(api_key="sk-test")
        assert verifier.cost_stats == {"calls": 0, "input_tokens": 0, "output_tokens": 0}

        await verifier.verify("a", "b")
        stats = verifier.cost_stats
        assert stats["calls"] == 1
        assert stats["input_tokens"] == 100
        assert stats["output_tokens"] == 50

        # Second call accumulates
        await verifier.verify("c", "d")
        stats = verifier.cost_stats
        assert stats["calls"] == 2
        assert stats["input_tokens"] == 200
        assert stats["output_tokens"] == 100

    @respx.mock
    async def test_empty_response_returns_false(self) -> None:
        respx.post(_ANTHROPIC_API_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "claude-3-haiku-20240307",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            )
        )
        verifier = LLMVerifier(api_key="sk-test")
        result = await verifier.verify("a", "b")
        assert result.equivalent is False
        assert result.reason == "empty_response"


class TestLLMVerifyResult:
    """Tests for the LLMVerifyResult model."""

    def test_defaults(self) -> None:
        result = LLMVerifyResult()
        assert result.equivalent is False
        assert result.confidence == 0.0
        assert result.reason == ""

    def test_with_values(self) -> None:
        result = LLMVerifyResult(equivalent=True, confidence=0.95, reason="match")
        assert result.equivalent is True
        assert result.confidence == 0.95
        assert result.reason == "match"
