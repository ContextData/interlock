"""Unit tests for the OIDC integration module."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from interlock.core.oidc import OIDCProvider, OIDCVerificationError, _decode_jwt_payload
from interlock.models import OIDCTokens, OIDCUserInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict) -> str:
    """Build a fake JWT (header.payload.signature) with given payload."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"fake-signature").rstrip(b"=")
    return f"{header.decode()}.{body.decode()}.{sig.decode()}"


def _mock_response(status_code: int, json_data: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://example.com"),
    )


# ---------------------------------------------------------------------------
# JWT decode
# ---------------------------------------------------------------------------


class TestDecodeJwtPayload:
    def test_valid_token(self):
        payload = {"sub": "user123", "email": "u@example.com"}
        token = _make_jwt(payload)
        decoded = _decode_jwt_payload(token)
        assert decoded["sub"] == "user123"
        assert decoded["email"] == "u@example.com"

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid JWT format"):
            _decode_jwt_payload("not.a.valid.jwt.with.too.many.parts")

    def test_two_segments_raises(self):
        with pytest.raises(ValueError, match="Invalid JWT format"):
            _decode_jwt_payload("only.two")


# ---------------------------------------------------------------------------
# OIDCProvider
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> OIDCProvider:
    p = OIDCProvider(
        issuer_url="https://idp.example.com",
        client_id="my-client",
        client_secret="my-secret",
        redirect_uri="http://localhost:9090/auth/callback",
    )
    # Manually set discovery results so tests don't need to call initialize()
    p._authorization_endpoint = "https://idp.example.com/authorize"
    p._token_endpoint = "https://idp.example.com/token"
    p._userinfo_endpoint = "https://idp.example.com/userinfo"
    p._jwks_uri = "https://idp.example.com/.well-known/jwks.json"
    p._initialized = True
    return p


class TestGetAuthorizationUrl:
    def test_contains_required_params(self, provider: OIDCProvider):
        url = provider.get_authorization_url(state="test-state")
        assert "response_type=code" in url
        assert "client_id=my-client" in url
        assert "state=test-state" in url
        assert "scope=openid+profile+email" in url
        assert url.startswith("https://idp.example.com/authorize?")

    def test_generates_state_when_none(self, provider: OIDCProvider):
        url = provider.get_authorization_url()
        assert "state=" in url

    def test_raises_when_not_initialized(self):
        p = OIDCProvider(
            issuer_url="https://x.com",
            client_id="c",
            client_secret="s",
        )
        with pytest.raises(RuntimeError, match="not initialized"):
            p.get_authorization_url()


class TestDiscoverySecurity:
    @pytest.mark.asyncio
    async def test_discovery_issuer_must_match_exactly(self):
        p = OIDCProvider("https://idp.example.com", "client", "secret")
        response = _mock_response(
            200,
            {
                "issuer": "https://idp.example.com/",
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token",
                "jwks_uri": "https://idp.example.com/jwks",
            },
        )
        with patch("interlock.core.oidc.httpx.AsyncClient") as client_class:
            client = AsyncMock()
            client.get.return_value = response
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client_class.return_value = client
            with pytest.raises(OIDCVerificationError, match="issuer"):
                await p.initialize()

    @pytest.mark.asyncio
    async def test_discovery_rejects_insecure_endpoints_by_default(self):
        p = OIDCProvider("https://idp.example.com", "client", "secret")
        response = _mock_response(
            200,
            {
                "issuer": "https://idp.example.com",
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "http://idp.example.com/token",
                "jwks_uri": "https://idp.example.com/jwks",
            },
        )
        with patch("interlock.core.oidc.httpx.AsyncClient") as client_class:
            client = AsyncMock()
            client.get.return_value = response
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client_class.return_value = client
            with pytest.raises(OIDCVerificationError, match="must use https"):
                await p.initialize()

    @pytest.mark.asyncio
    async def test_discovery_requires_jwks_endpoint(self):
        p = OIDCProvider("https://idp.example.com", "client", "secret")
        response = _mock_response(
            200,
            {
                "issuer": "https://idp.example.com",
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token",
            },
        )
        with patch("interlock.core.oidc.httpx.AsyncClient") as client_class:
            client = AsyncMock()
            client.get.return_value = response
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client_class.return_value = client
            with pytest.raises(OIDCVerificationError, match="jwks_uri"):
                await p.initialize()


class TestExchangeCode:
    @pytest.mark.asyncio
    async def test_returns_tokens(self, provider: OIDCProvider):
        token_response = {
            "access_token": "at_xyz",
            "id_token": "id_abc",
            "refresh_token": "rt_123",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

        mock_resp = _mock_response(200, token_response)

        with patch("interlock.core.oidc.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = instance

            tokens = await provider.exchange_code(
                "auth-code-123", code_verifier="test-pkce-verifier"
            )

        assert isinstance(tokens, OIDCTokens)
        assert tokens.access_token == "at_xyz"
        assert tokens.id_token == "id_abc"
        assert tokens.refresh_token == "rt_123"
        assert tokens.expires_in == 3600
        sent = instance.post.await_args.kwargs["data"]
        assert sent["code_verifier"] == "test-pkce-verifier"


class TestVerifyToken:
    @pytest.mark.asyncio
    async def test_decodes_claims(self, provider: OIDCProvider):
        # P1-F regression: verify_token now requires a JWKS-validated
        # signature. The unsigned-token path is gone. Comprehensive
        # JWKS-based tests live in test_p1_f_oidc_jwks.py.
        pytest.skip("Replaced by JWKS-based tests in test_p1_f_oidc_jwks.py")


class TestGetUserInfo:
    @pytest.mark.asyncio
    async def test_returns_user_info(self, provider: OIDCProvider):
        userinfo_data = {
            "sub": "user-99",
            "email": "u99@example.com",
            "name": "User 99",
            "preferred_username": "u99",
            "groups": ["team-a"],
        }

        mock_resp = _mock_response(200, userinfo_data)

        with patch("interlock.core.oidc.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = instance

            info = await provider.get_user_info("access-token-abc")

        assert isinstance(info, OIDCUserInfo)
        assert info.sub == "user-99"
        assert info.email == "u99@example.com"
        assert info.groups == ["team-a"]

    @pytest.mark.asyncio
    async def test_raises_when_not_initialized(self):
        p = OIDCProvider(
            issuer_url="https://x.com",
            client_id="c",
            client_secret="s",
        )
        with pytest.raises(RuntimeError, match="not initialized"):
            await p.get_user_info("tok")
