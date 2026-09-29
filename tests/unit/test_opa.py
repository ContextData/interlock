"""Unit tests for the OPA policy integration client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from interlock.core.opa import OPAClient
from interlock.models import AgentType, IdentityContext, PolicyDecision


def _make_identity(**overrides) -> IdentityContext:
    defaults = {
        "identity_id": 1,
        "user": "testuser",
        "agent_type": AgentType.CLAUDE_CODE,
        "roles": ["analyst"],
    }
    defaults.update(overrides)
    return IdentityContext(**defaults)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestOPAClientInitialize:
    @pytest.mark.asyncio
    async def test_initialize_available(self):
        client = OPAClient(opa_url="http://localhost:8181")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            await client.initialize()

        assert client.available is True
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_initialize_unreachable(self):
        client = OPAClient(opa_url="http://localhost:9999")

        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            await client.initialize()

        assert client.available is False
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_initialize_unhealthy_status(self):
        client = OPAClient(opa_url="http://localhost:8181")

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            await client.initialize()

        assert client.available is False
        await client.shutdown()


# ---------------------------------------------------------------------------
# Evaluate - fallback behavior
# ---------------------------------------------------------------------------


class TestOPAEvaluateFallback:
    @pytest.mark.asyncio
    async def test_not_available_returns_none(self):
        """When OPA is not available and fallback is enabled, return None."""
        client = OPAClient(fallback_to_builtin=True)
        # Never initialized - _available is False
        result = await client.evaluate(
            identity=_make_identity(),
            source_id="warehouse",
            operation="read",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_not_available_no_fallback_raises(self):
        """When OPA is not available and fallback is disabled, raise."""
        client = OPAClient(fallback_to_builtin=False)
        with pytest.raises(RuntimeError, match="not available"):
            await client.evaluate(
                identity=_make_identity(),
                source_id="warehouse",
                operation="read",
            )

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """On OPA timeout with fallback enabled, return None."""
        client = OPAClient(timeout_seconds=0.1, fallback_to_builtin=True)
        client._available = True
        client._client = httpx.AsyncClient(timeout=0.1)

        with patch.object(
            client._client,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timed out"),
        ):
            result = await client.evaluate(
                identity=_make_identity(),
                source_id="warehouse",
                operation="read",
            )

        assert result is None
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_timeout_no_fallback_raises(self):
        """On OPA timeout with fallback disabled, raise."""
        client = OPAClient(timeout_seconds=0.1, fallback_to_builtin=False)
        client._available = True
        client._client = httpx.AsyncClient(timeout=0.1)

        with (
            patch.object(
                client._client,
                "post",
                new_callable=AsyncMock,
                side_effect=httpx.TimeoutException("timed out"),
            ),
            pytest.raises(httpx.TimeoutException),
        ):
            await client.evaluate(
                identity=_make_identity(),
                source_id="warehouse",
                operation="read",
            )

        await client.shutdown()

    @pytest.mark.asyncio
    async def test_opa_error_returns_none(self):
        """On OPA HTTP error with fallback, return None."""
        client = OPAClient(fallback_to_builtin=True)
        client._available = True
        client._client = httpx.AsyncClient()

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )

        with patch.object(
            client._client, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await client.evaluate(
                identity=_make_identity(),
                source_id="warehouse",
                operation="read",
            )

        assert result is None
        await client.shutdown()


# ---------------------------------------------------------------------------
# Evaluate - successful OPA responses
# ---------------------------------------------------------------------------


class TestOPAEvaluateSuccess:
    @pytest.mark.asyncio
    async def test_allow_response(self):
        client = OPAClient()
        client._available = True
        client._client = httpx.AsyncClient()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "result": {"allow": True, "reason": "analyst read access"}
        }

        with patch.object(
            client._client, "post", new_callable=AsyncMock, return_value=mock_response
        ) as mock_post:
            result = await client.evaluate(
                identity=_make_identity(roles=["analyst"]),
                source_id="warehouse",
                operation="read",
                tables=["orders"],
            )

        assert isinstance(result, PolicyDecision)
        assert result.allowed is True
        assert result.reason == "analyst read access"

        # Verify the input document structure
        call_args = mock_post.call_args
        input_doc = call_args.kwargs["json"]["input"]
        assert input_doc["source_id"] == "warehouse"
        assert input_doc["operation"] == "read"
        assert input_doc["tables"] == ["orders"]
        assert input_doc["identity"]["roles"] == ["analyst"]
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_deny_response(self):
        client = OPAClient()
        client._available = True
        client._client = httpx.AsyncClient()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "result": {"allow": False, "reason": "insufficient privileges"}
        }

        with patch.object(
            client._client, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await client.evaluate(
                identity=_make_identity(roles=["viewer"]),
                source_id="warehouse",
                operation="write",
            )

        assert isinstance(result, PolicyDecision)
        assert result.allowed is False
        assert result.reason == "insufficient privileges"
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_deny_without_reason_gets_default(self):
        client = OPAClient()
        client._available = True
        client._client = httpx.AsyncClient()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"result": {"allow": False}}

        with patch.object(
            client._client, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await client.evaluate(
                identity=_make_identity(),
                source_id="warehouse",
                operation="write",
            )

        assert result is not None
        assert result.allowed is False
        assert result.reason == "OPA denied"
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_columns_included_in_input(self):
        client = OPAClient()
        client._available = True
        client._client = httpx.AsyncClient()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"result": {"allow": True}}

        with patch.object(
            client._client, "post", new_callable=AsyncMock, return_value=mock_response
        ) as mock_post:
            await client.evaluate(
                identity=_make_identity(),
                source_id="warehouse",
                operation="read",
                tables=["users"],
                columns=["email", "name"],
            )

        input_doc = mock_post.call_args.kwargs["json"]["input"]
        assert input_doc["columns"] == ["email", "name"]
        await client.shutdown()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestOPAShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_clears_state(self):
        client = OPAClient()
        client._available = True
        client._client = httpx.AsyncClient()

        await client.shutdown()
        assert client.available is False
        assert client._client is None

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self):
        client = OPAClient()
        await client.shutdown()  # No client, should not error
        assert client.available is False
