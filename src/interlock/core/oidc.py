"""OpenID Connect integration for enterprise identity providers.

AUDIT-COVERS: P1-F

Hardening over the original implementation:

- JWKS signature verification via authlib's ``JsonWebToken``. The keys
  are fetched from the provider's ``jwks_uri`` and cached. Unsigned or
  forged tokens raise.
- Required claim validation: ``iss`` must equal the configured issuer,
  ``aud`` must equal the client_id, ``exp`` must be in the future,
  ``nbf`` (if present) must be in the past, and a configurable
  ``nonce`` may be required.
- ``state`` is generated at authorization-URL time and the caller is
  expected to verify it on callback (helper provided).

The unsigned base64 decode helper is preserved for diagnostic logging
but is no longer used by ``verify_token``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx

from interlock.models import OIDCTokens, OIDCUserInfo

logger = logging.getLogger(__name__)


class OIDCVerificationError(Exception):
    """Raised when an ID token fails signature or claim validation."""


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload segment of a JWT *without* signature verification.

    Diagnostic / logging use only. ``OIDCProvider.verify_token`` uses
    authlib's verifier with JWKS keys.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format: expected 3 dot-separated segments")
    payload_b64 = parts[1]
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


class OIDCProvider:
    """OpenID Connect integration for enterprise identity providers."""

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str = "http://localhost:9090/auth/callback",
        scopes: list[str] | None = None,
        jwks_cache_ttl_seconds: int = 3600,
        allow_insecure_endpoints: bool = False,
    ) -> None:
        self._issuer_url = issuer_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scopes = scopes or ["openid", "profile", "email"]
        self._jwks_cache_ttl = jwks_cache_ttl_seconds
        self._allow_insecure_endpoints = allow_insecure_endpoints

        # Populated by initialize()
        self._authorization_endpoint: str | None = None
        self._token_endpoint: str | None = None
        self._userinfo_endpoint: str | None = None
        self._jwks_uri: str | None = None
        self._initialized = False

        # JWKS cache.
        self._jwks: dict | None = None
        self._jwks_fetched_at: float = 0.0

    async def initialize(self) -> None:
        """Discover OIDC configuration from .well-known endpoint."""
        if (
            not self._allow_insecure_endpoints
            and urlsplit(self._issuer_url).scheme.lower() != "https"
        ):
            raise OIDCVerificationError("OIDC issuer must use https")
        url = f"{self._issuer_url.rstrip('/')}/.well-known/openid-configuration"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            config = resp.json()

        if config.get("issuer") != self._issuer_url:
            raise OIDCVerificationError("OIDC discovery issuer does not match configured issuer")
        required_endpoints: tuple[str, ...] = (
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
        )
        for name in required_endpoints:
            if not config.get(name):
                raise OIDCVerificationError(f"OIDC discovery is missing {name}")
        endpoint_names = required_endpoints
        if not self._allow_insecure_endpoints:
            endpoint_names = endpoint_names + ("userinfo_endpoint",)
            for name in endpoint_names:
                value = config.get(name)
                if value and urlsplit(str(value)).scheme.lower() != "https":
                    raise OIDCVerificationError(f"OIDC discovery {name} must use https")

        self._authorization_endpoint = config["authorization_endpoint"]
        self._token_endpoint = config["token_endpoint"]
        self._userinfo_endpoint = config.get("userinfo_endpoint")
        self._jwks_uri = config.get("jwks_uri")
        self._initialized = True
        logger.info("OIDC provider initialized from %s", url)

    def get_authorization_url(
        self,
        state: str | None = None,
        *,
        nonce: str | None = None,
        code_challenge: str | None = None,
    ) -> str:
        """Generate the authorization URL for redirect."""
        if not self._initialized or not self._authorization_endpoint:
            raise RuntimeError("OIDCProvider not initialized - call initialize() first")

        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(self._scopes),
            "state": state or secrets.token_urlsafe(32),
        }
        if nonce:
            params["nonce"] = nonce
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self._authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(self, code: str, *, code_verifier: str) -> OIDCTokens:
        """Exchange an authorization code for tokens."""
        if not self._initialized or not self._token_endpoint:
            raise RuntimeError("OIDCProvider not initialized - call initialize() first")

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code_verifier": code_verifier,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(self._token_endpoint, data=data)
            resp.raise_for_status()
            body = resp.json()

        return OIDCTokens(
            access_token=body["access_token"],
            id_token=body.get("id_token"),
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            token_type=body.get("token_type", "Bearer"),
        )

    async def _load_jwks(self, force: bool = False) -> dict:
        """Fetch and cache the provider's JWKS document."""
        if not self._jwks_uri:
            raise OIDCVerificationError("Provider has no jwks_uri configured")
        now = time.time()
        if (
            not force
            and self._jwks is not None
            and (now - self._jwks_fetched_at) < self._jwks_cache_ttl
        ):
            return self._jwks
        async with httpx.AsyncClient() as client:
            resp = await client.get(self._jwks_uri)
            resp.raise_for_status()
            self._jwks = resp.json()
        self._jwks_fetched_at = now
        return self._jwks

    async def verify_token(
        self,
        id_token: str,
        *,
        nonce: str | None = None,
        leeway_seconds: int = 30,
    ) -> OIDCUserInfo:
        """Verify a JWT signature against JWKS and validate claims.

        Raises ``OIDCVerificationError`` for any failure.
        """
        try:
            from authlib.jose import JsonWebToken
            from authlib.jose.errors import JoseError
        except ImportError as exc:  # pragma: no cover - ship dep guarantees this
            raise OIDCVerificationError("authlib not available") from exc

        # Try cached JWKS first; on KID-not-found, force a refresh.
        jwks = await self._load_jwks()

        jwt = JsonWebToken(["RS256", "RS384", "RS512", "ES256", "ES384"])
        try:
            claims = jwt.decode(id_token, key=jwks)
        except (JoseError, ValueError, KeyError) as first_err:
            # Either the signature is wrong, the kid does not match the
            # cached JWKS, or the JWKS shape is malformed. Re-fetch JWKS
            # once in case keys have rotated, then try again.
            try:
                jwks = await self._load_jwks(force=True)
                claims = jwt.decode(id_token, key=jwks)
            except (JoseError, ValueError, KeyError) as exc:
                raise OIDCVerificationError(f"JWT signature verification failed: {exc}") from exc
            except Exception as exc:
                raise OIDCVerificationError(
                    f"JWT signature verification failed: {first_err}"
                ) from exc

        # Required claims and equality checks.
        now = int(time.time())
        if claims.get("iss") != self._issuer_url:
            raise OIDCVerificationError(
                f"iss mismatch: got {claims.get('iss')!r}, expected {self._issuer_url!r}"
            )
        aud = claims.get("aud")
        if isinstance(aud, list):
            if self._client_id not in aud:
                raise OIDCVerificationError(f"aud {aud!r} does not include client_id")
            if len(aud) > 1 and claims.get("azp") != self._client_id:
                raise OIDCVerificationError("multi-audience token requires matching azp")
        elif aud != self._client_id:
            raise OIDCVerificationError(f"aud {aud!r} != client_id {self._client_id!r}")
        exp = claims.get("exp")
        if exp is None or int(exp) + leeway_seconds < now:
            raise OIDCVerificationError("Token expired or missing exp")
        nbf = claims.get("nbf")
        if nbf is not None and int(nbf) - leeway_seconds > now:
            raise OIDCVerificationError("Token not yet valid (nbf)")
        if nonce is not None and claims.get("nonce") != nonce:
            raise OIDCVerificationError("nonce mismatch")

        return OIDCUserInfo(
            sub=claims["sub"],
            email=claims.get("email"),
            name=claims.get("name"),
            preferred_username=claims.get("preferred_username"),
            groups=claims.get("groups", []),
        )

    @staticmethod
    def make_state() -> str:
        """Generate a cryptographically random state value for CSRF defence."""
        return secrets.token_urlsafe(32)

    @staticmethod
    def make_pkce_pair() -> tuple[str, str]:
        """Return an RFC 7636 verifier and S256 challenge."""
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return verifier, challenge

    @staticmethod
    def verify_state(received: str, expected: str) -> bool:
        """Constant-time equality for OAuth state validation."""
        return bool(received) and hmac.compare_digest(received, expected)

    async def get_user_info(self, access_token: str) -> OIDCUserInfo:
        """Get user info from the userinfo endpoint."""
        if not self._initialized or not self._userinfo_endpoint:
            raise RuntimeError("OIDCProvider not initialized or userinfo endpoint not available")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self._userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return OIDCUserInfo(
            sub=data["sub"],
            email=data.get("email"),
            name=data.get("name"),
            preferred_username=data.get("preferred_username"),
            groups=data.get("groups", []),
        )
