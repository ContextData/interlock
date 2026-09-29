"""Regression test for audit P1-F: OIDC JWKS signature verification.

AUDIT-COVERS: P1-F

The audit reported that OIDCProvider.verify_token only base64-decoded
the payload and explicitly noted production needed JWKS validation.

These tests use authlib's real RSA key generation and ``JsonWebKey`` to
sign a JWT, then point ``OIDCProvider`` at a mocked JWKS endpoint and
assert:

- A correctly signed token with valid claims is decoded.
- A token signed with the wrong key is rejected.
- A token with mismatched ``iss`` / ``aud`` is rejected.
- An expired token is rejected.
- A token with the wrong ``nonce`` is rejected.
- ``state`` helpers generate random values and equality is constant-time.
"""

from __future__ import annotations

import time

import pytest
from authlib.jose import JsonWebKey, JsonWebToken

from interlock.core.oidc import OIDCProvider, OIDCVerificationError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_provider(jwks: dict) -> OIDCProvider:
    p = OIDCProvider(
        issuer_url="https://idp.example.com",
        client_id="interlock-admin",
        client_secret="ignored-by-verify",
    )
    p._authorization_endpoint = "https://idp.example.com/auth"
    p._token_endpoint = "https://idp.example.com/token"
    p._userinfo_endpoint = "https://idp.example.com/userinfo"
    p._jwks_uri = "https://idp.example.com/jwks"
    p._initialized = True
    # Pre-populate the JWKS cache so verify_token never hits the network.
    p._jwks = jwks
    p._jwks_fetched_at = time.time()
    return p


def _generate_signed_token(claims: dict, kid: str = "kid-1") -> tuple[str, dict]:
    """Generate an RSA key, sign a JWT, return (token, jwks)."""
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    private_dict = key.as_dict(is_private=True)
    private_dict["kid"] = kid

    public_dict = key.as_dict(is_private=False)
    public_dict["kid"] = kid
    public_dict.setdefault("use", "sig")
    public_dict.setdefault("alg", "RS256")

    jwt = JsonWebToken(["RS256"])
    header = {"alg": "RS256", "kid": kid}
    token_bytes = jwt.encode(header, claims, private_dict)
    token = token_bytes.decode() if isinstance(token_bytes, bytes) else token_bytes
    jwks = {"keys": [public_dict]}
    return token, jwks


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_f_valid_signed_token_is_decoded() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "interlock-admin",
        "sub": "user-1",
        "email": "alice@example.com",
        "groups": ["admin"],
        "exp": now + 600,
        "iat": now,
        "nonce": "n-1",
    }
    token, jwks = _generate_signed_token(claims)
    provider = _make_provider(jwks)

    info = await provider.verify_token(token, nonce="n-1")
    assert info.sub == "user-1"
    assert info.email == "alice@example.com"
    assert info.groups == ["admin"]


@pytest.mark.asyncio
async def test_p1_f_token_signed_with_wrong_key_rejected() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "interlock-admin",
        "sub": "user-1",
        "exp": now + 600,
    }
    # Sign with one key, advertise a different key in JWKS.
    forged_token, _ = _generate_signed_token(claims, kid="kid-A")
    _, real_jwks = _generate_signed_token(claims, kid="kid-B")
    provider = _make_provider(real_jwks)

    with pytest.raises(OIDCVerificationError):
        await provider.verify_token(forged_token)


@pytest.mark.asyncio
async def test_p1_f_wrong_issuer_rejected() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://attacker.example.com",
        "aud": "interlock-admin",
        "sub": "user-1",
        "exp": now + 600,
    }
    token, jwks = _generate_signed_token(claims)
    provider = _make_provider(jwks)
    with pytest.raises(OIDCVerificationError) as exc:
        await provider.verify_token(token)
    assert "iss" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_p1_f_wrong_audience_rejected() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "some-other-app",
        "sub": "user-1",
        "exp": now + 600,
    }
    token, jwks = _generate_signed_token(claims)
    provider = _make_provider(jwks)
    with pytest.raises(OIDCVerificationError) as exc:
        await provider.verify_token(token)
    assert "aud" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_p1_f_expired_token_rejected() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "interlock-admin",
        "sub": "user-1",
        "exp": now - 600,
    }
    token, jwks = _generate_signed_token(claims)
    provider = _make_provider(jwks)
    with pytest.raises(OIDCVerificationError) as exc:
        await provider.verify_token(token)
    assert "exp" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_p1_f_wrong_nonce_rejected() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "interlock-admin",
        "sub": "user-1",
        "exp": now + 600,
        "nonce": "n-good",
    }
    token, jwks = _generate_signed_token(claims)
    provider = _make_provider(jwks)
    with pytest.raises(OIDCVerificationError):
        await provider.verify_token(token, nonce="n-bad")


@pytest.mark.asyncio
async def test_p1_f_multi_audience_requires_matching_azp() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": ["other-client", "interlock-admin"],
        "sub": "user-1",
        "exp": now + 600,
    }
    token, jwks = _generate_signed_token(claims)
    provider = _make_provider(jwks)
    with pytest.raises(OIDCVerificationError, match="azp"):
        await provider.verify_token(token)


@pytest.mark.asyncio
async def test_p1_f_multi_audience_matching_azp_accepted() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": ["other-client", "interlock-admin"],
        "azp": "interlock-admin",
        "sub": "user-1",
        "exp": now + 600,
    }
    token, jwks = _generate_signed_token(claims)
    provider = _make_provider(jwks)
    info = await provider.verify_token(token)
    assert info.sub == "user-1"


@pytest.mark.asyncio
async def test_p1_f_trailing_slash_issuer_variant_rejected() -> None:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com/",
        "aud": "interlock-admin",
        "sub": "user-1",
        "exp": now + 600,
    }
    token, jwks = _generate_signed_token(claims)
    provider = _make_provider(jwks)
    with pytest.raises(OIDCVerificationError, match="iss mismatch"):
        await provider.verify_token(token)


def test_p1_f_state_helpers_generate_and_verify() -> None:
    s1 = OIDCProvider.make_state()
    s2 = OIDCProvider.make_state()
    assert s1 != s2
    assert OIDCProvider.verify_state(s1, s1) is True
    assert OIDCProvider.verify_state(s1, s2) is False
    assert OIDCProvider.verify_state("", s1) is False


def test_pkce_pair_is_s256_and_has_required_entropy() -> None:
    import base64
    import hashlib

    verifier, challenge = OIDCProvider.make_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert len(verifier) >= 43
    assert challenge == expected
