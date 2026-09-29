"""Versioned API-key hashing and validation."""

from __future__ import annotations

import hashlib
import hmac
import re

from interlock.config import AuthConfig

LEGACY_SHA256 = "sha256-v1"
HMAC_SHA256 = "hmac-sha256-v2"
_SAFE_KEY = re.compile(r"^[!-~]+$")
_FORBIDDEN = frozenset({'"', "'", "`", "<", ">", "\\"})


def legacy_api_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def hmac_api_key_hash(api_key: str, pepper: str) -> str:
    if not pepper:
        raise ValueError("API-key pepper is required for HMAC hashing")
    return hmac.new(pepper.encode("utf-8"), api_key.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_api_key_for_storage(api_key: str, config: AuthConfig) -> tuple[str, str]:
    if config.api_key_pepper:
        return hmac_api_key_hash(api_key, config.api_key_pepper), HMAC_SHA256
    return legacy_api_key_hash(api_key), LEGACY_SHA256


def candidate_api_key_hashes(api_key: str, config: AuthConfig) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if config.api_key_pepper:
        candidates.append((hmac_api_key_hash(api_key, config.api_key_pepper), HMAC_SHA256))
    if config.allow_legacy_sha256_keys or not candidates:
        candidates.append((legacy_api_key_hash(api_key), LEGACY_SHA256))
    return candidates


def validate_custom_api_key(api_key: str, config: AuthConfig) -> bool:
    if len(api_key) < config.custom_api_key_min_length:
        return False
    if not _SAFE_KEY.fullmatch(api_key):
        return False
    if any(char in api_key for char in _FORBIDDEN):
        return False
    return len(set(api_key)) >= 8
