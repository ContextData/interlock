from __future__ import annotations

import ipaddress
from unittest.mock import AsyncMock

import pytest

from interlock.security.egress import (
    EgressBlockedError,
    PinnedSafeNetworkBackend,
    join_and_validate_http_egress_url,
    validate_host_egress,
    validate_http_egress_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost/admin",
        "http://10.0.0.1/admin",
        "http://172.16.0.1/admin",
        "http://192.168.1.10/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://2130706433/admin",
        "http://0x7f000001/admin",
        "http://017700000001/admin",
        "http://0177.0.0.1/admin",
        "http://0x7f.0.0.1/admin",
        "http://%31%32%37.0.0.1/admin",
        "http://metadata.google.internal/computeMetadata/v1",
        "ftp://example.com/file",
    ],
)
def test_private_or_non_http_egress_targets_are_blocked(url: str) -> None:
    with pytest.raises(EgressBlockedError):
        validate_http_egress_url(url)


def test_public_https_egress_target_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "interlock.security.egress.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    validate_http_egress_url("https://api.example.com/v1/customers")


def test_private_egress_can_be_explicitly_allowed_for_local_mocks() -> None:
    validate_http_egress_url("http://127.0.0.1:8080/health", allow_private=True)


def test_dns_resolution_to_private_ip_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "interlock.security.egress.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("169.254.169.254", 80))],
    )
    with pytest.raises(EgressBlockedError):
        validate_http_egress_url("http://internal.example.com")


def test_unresolved_host_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_resolution(*_args, **_kwargs):
        raise OSError("dns unavailable")

    monkeypatch.setattr("interlock.security.egress.socket.getaddrinfo", fail_resolution)
    with pytest.raises(EgressBlockedError):
        validate_host_egress("api.example.com")


def test_joined_connector_path_cannot_escape_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "interlock.security.egress.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

    with pytest.raises(EgressBlockedError):
        join_and_validate_http_egress_url("https://api.example.com/v1", "../admin")

    with pytest.raises(EgressBlockedError):
        join_and_validate_http_egress_url("https://api.example.com/v1", "%2e%2e/admin")

    assert (
        join_and_validate_http_egress_url("https://api.example.com/v1", "customers")
        == "https://api.example.com/v1/customers"
    )


@pytest.mark.asyncio
async def test_pinned_backend_connects_to_validated_address_not_hostname(monkeypatch) -> None:
    backend = PinnedSafeNetworkBackend()
    backend._backend = AsyncMock()
    backend._backend.connect_tcp.return_value = object()
    monkeypatch.setattr(
        "interlock.security.egress._resolve_host_addresses",
        lambda _host, _port: {ipaddress.ip_address("93.184.216.34")},
    )

    await backend.connect_tcp("api.example.com", 443)

    backend._backend.connect_tcp.assert_awaited_once()
    assert backend._backend.connect_tcp.await_args.args[:2] == ("93.184.216.34", 443)


@pytest.mark.asyncio
async def test_pinned_backend_blocks_dns_rebinding_to_private_peer(monkeypatch) -> None:
    backend = PinnedSafeNetworkBackend()
    backend._backend = AsyncMock()
    monkeypatch.setattr(
        "interlock.security.egress._resolve_host_addresses",
        lambda _host, _port: {ipaddress.ip_address("169.254.169.254")},
    )

    with pytest.raises(EgressBlockedError):
        await backend.connect_tcp("api.example.com", 443)

    backend._backend.connect_tcp.assert_not_awaited()
