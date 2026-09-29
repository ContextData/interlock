from __future__ import annotations

from interlock.config import AuthConfig
from interlock.security.api_keys import (
    HMAC_SHA256,
    candidate_api_key_hashes,
    hash_api_key_for_storage,
    validate_custom_api_key,
)


def test_peppered_hash_is_stable_and_versioned() -> None:
    config = AuthConfig(api_key_pepper="pepper-" * 6)
    first = hash_api_key_for_storage("interlock_" + "aB3-" * 10, config)
    second = hash_api_key_for_storage("interlock_" + "aB3-" * 10, config)
    assert first == second
    assert first[1] == HMAC_SHA256


def test_candidate_hashes_support_bounded_legacy_transition() -> None:
    config = AuthConfig(api_key_pepper="pepper-" * 6, allow_legacy_sha256_keys=True)
    candidates = candidate_api_key_hashes("interlock_" + "aB3-" * 10, config)
    assert [version for _, version in candidates] == [HMAC_SHA256, "sha256-v1"]


def test_custom_api_key_entropy_policy() -> None:
    config = AuthConfig(custom_api_key_min_length=32)
    assert validate_custom_api_key("interlock_" + "aB3-" * 10, config)
    assert not validate_custom_api_key("short", config)
    assert not validate_custom_api_key("x" * 64, config)
    assert not validate_custom_api_key("safe-looking-key-1234567890<script>", config)
