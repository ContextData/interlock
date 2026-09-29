from __future__ import annotations

import json

import pytest
from starlette.datastructures import Headers

from interlock.core.approval_queue import _decode_http_body
from interlock.errors import InterLockError
from interlock.gateway.http_proxy import (
    _apply_policy_json_redaction,
    _approval_body_metadata,
    _build_upstream_url,
    _filter_request_headers,
    _filter_response_headers,
    _http_cache_key,
    _http_governance_scope_hash,
)
from interlock.gateway.pipeline import GatewayDecision
from interlock.models import PolicyDecision


def test_http_audit_body_redacts_secret_like_fields() -> None:
    metadata = _approval_body_metadata(
        b'{"username":"ada","password":"secret","nested":{"api_key":"key"}}'
    )

    assert metadata is not None
    assert metadata["encoding"] == "json"
    assert metadata["data"] == {
        "username": "ada",
        "password": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]"},
    }
    assert metadata["truncated"] is False


def test_http_audit_text_body_redacts_secret_like_fields() -> None:
    metadata = _approval_body_metadata(b"token=abc123&safe=value")

    assert metadata is not None
    assert metadata["encoding"] == "text"
    assert metadata["data"] == "token=[REDACTED]&safe=value"


def test_http_audit_large_json_body_is_limited_and_not_executable() -> None:
    metadata = _approval_body_metadata(
        json.dumps({"password": "secret", "notes": "x" * 5000}).encode()
    )

    assert metadata is not None
    assert metadata["encoding"] == "truncated"
    assert metadata["truncated"] is True
    assert len(str(metadata["data"])) <= 4096
    assert "secret" not in str(metadata["data"])
    with pytest.raises(InterLockError, match="truncated"):
        _decode_http_body(metadata)


def test_policy_json_redaction_applies_to_nested_response_fields() -> None:
    body = json.dumps(
        {
            "id": 1,
            "ssn": "123-45-6789",
            "nested": [{"email": "ada@example.com", "safe": "ok"}],
        }
    ).encode()

    redacted, stats = _apply_policy_json_redaction(body, ["ssn", "email"])

    payload = json.loads(redacted)
    assert payload["ssn"] == "[REDACTED:POLICY]"
    assert payload["nested"][0]["email"] == "[REDACTED:POLICY]"
    assert payload["nested"][0]["safe"] == "ok"
    assert stats == {
        "policy_fields": ["email", "ssn"],
        "redacted_fields": ["email", "ssn"],
        "count": 2,
    }


def test_http_upstream_url_builder_preserves_configured_base_path() -> None:
    assert (
        _build_upstream_url("https://api.example.com/v1/base/", "customers/42")
        == "https://api.example.com/v1/base/customers/42"
    )


def test_http_upstream_url_builder_rejects_encoded_traversal() -> None:
    with pytest.raises(Exception, match="invalid_path"):
        _build_upstream_url("https://api.example.com/v1/base", "%2e%2e/admin")


def test_http_request_header_filter_is_allowlist_based() -> None:
    filtered = _filter_request_headers(
        Headers(
            {
                "Authorization": "Bearer key",
                "Cookie": "sid=secret",
                "If-Match": '"v1"',
                "X-Forwarded-For": "10.0.0.5",
            }
        ),
        allowlist=frozenset({"if-match"}),
    )

    assert filtered == {"if-match": '"v1"'}


def test_http_response_header_filter_strips_unsafe_headers() -> None:
    filtered = _filter_response_headers(
        Headers(
            {
                "Content-Type": "application/json",
                "Location": "http://169.254.169.254/latest",
                "Set-Cookie": "sid=secret",
                "X-Powered-By": "upstream",
            }
        ),
        allowlist=frozenset({"content-type", "location", "set-cookie"}),
    )

    assert filtered == {"content-type": "application/json"}


def _cache_key(**overrides: object) -> str:
    values: dict[str, object] = {
        "source_id": "source-1",
        "method": "GET",
        "path": "customers/42",
        "query": "expand=orders",
        "identity_id": 101,
        "role": "upstream_reader",
        "team": "risk",
        "grants_version": "grant-v7",
        "governance_scope_hash": "scope-a",
        "source_generation": 9,
    }
    values.update(overrides)
    return _http_cache_key(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "different"),
    [
        ("identity_id", 102),
        ("role", "upstream_admin"),
        ("team", "finance"),
        ("grants_version", "grant-v8"),
        ("governance_scope_hash", "scope-b"),
        ("source_generation", 10),
    ],
)
def test_http_cache_key_isolates_every_governance_dimension(
    field: str,
    different: object,
) -> None:
    assert _cache_key() != _cache_key(**{field: different})


def test_http_governance_scope_hash_includes_policy_redaction() -> None:
    visible = GatewayDecision(
        allowed=True,
        policy_decision=PolicyDecision(allowed=True, redact_columns=[]),
        redaction_required=True,
    )
    redacted = GatewayDecision(
        allowed=True,
        policy_decision=PolicyDecision(allowed=True, redact_columns=["ssn"]),
        redaction_required=True,
    )

    assert _http_governance_scope_hash(visible) != _http_governance_scope_hash(redacted)
