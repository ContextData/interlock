"""LLM verification gate for semantic cache medium-confidence matches."""

from __future__ import annotations

import json
import logging
import os

import httpx

from interlock.models import LLMVerifyResult

logger = logging.getLogger(__name__)

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM_PROMPT = (
    "You are a query equivalence judge. Given two database query intents, "
    "determine if they are semantically equivalent - meaning they ask for "
    "the same data with the same filters, grouping, and ordering. "
    'Respond with JSON only: {"equivalent": bool, "confidence": float 0-1, "reason": str}'
)

_USER_TEMPLATE = (
    "Are these two query intents semantically equivalent?\n\n"
    "Intent A: {original}\n\n"
    "Intent B: {cached}\n\n"
    "Respond with JSON only."
)


class LLMVerifier:
    """Ask an LLM whether two query intents are semantically equivalent.

    Used when the semantic cache returns a match with medium confidence
    (between per-source thresholds). On any error the safe default is
    equivalent=False (cache miss).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-3-haiku-20240307",
        timeout_ms: int = 500,
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model
        self._timeout_ms = timeout_ms
        self._available = bool(self._api_key)
        self._cost_tracker: dict[str, int] = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    # -- public API -----------------------------------------------------------

    async def verify(
        self,
        original_intent: str,
        cached_intent: str,
    ) -> LLMVerifyResult:
        """Ask LLM if two intents are semantically equivalent.

        Returns LLMVerifyResult. On timeout or API error returns
        equivalent=False (safe default - cache miss).
        """
        if not self._available:
            return LLMVerifyResult(reason="no_api_key")

        try:
            return await self._call_api(original_intent, cached_intent)
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            logger.debug("LLM verify failed: %s", exc)
            return LLMVerifyResult(reason=f"api_error: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLM verify unexpected error: %s", exc)
            return LLMVerifyResult(reason=f"unexpected_error: {type(exc).__name__}")

    @property
    def available(self) -> bool:
        return self._available

    @property
    def cost_stats(self) -> dict[str, int]:
        return dict(self._cost_tracker)

    # -- internals ------------------------------------------------------------

    async def _call_api(
        self,
        original_intent: str,
        cached_intent: str,
    ) -> LLMVerifyResult:
        timeout = httpx.Timeout(self._timeout_ms / 1000.0, connect=5.0)
        payload = {
            "model": self._model,
            "max_tokens": 256,
            "system": _SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        original=original_intent,
                        cached=cached_intent,
                    ),
                }
            ],
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

        # Track token usage
        usage = body.get("usage", {})
        self._cost_tracker["calls"] += 1
        self._cost_tracker["input_tokens"] += usage.get("input_tokens", 0)
        self._cost_tracker["output_tokens"] += usage.get("output_tokens", 0)

        # Extract text content from the response
        content_blocks = body.get("content", [])
        if not content_blocks:
            return LLMVerifyResult(reason="empty_response")

        text = content_blocks[0].get("text", "")
        return self._parse_response(text)

    @staticmethod
    def _parse_response(text: str) -> LLMVerifyResult:
        """Parse JSON from the LLM response text."""
        try:
            data = json.loads(text)
            return LLMVerifyResult(
                equivalent=bool(data.get("equivalent", False)),
                confidence=float(data.get("confidence", 0.0)),
                reason=str(data.get("reason", "")),
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.debug("Failed to parse LLM response: %s", exc)
            return LLMVerifyResult(reason=f"parse_error: {exc}")
