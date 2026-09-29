"""Outbound URL guardrails for proxy, approvals, probes, and connectors."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import quote, unquote, urljoin, urlparse

from interlock.errors import EgressBlockedError

_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}
_METADATA_HOSTNAMES = {
    "metadata",
    "metadata.google.internal",
    "169.254.169.254",
}
_NUMERIC_IPV4_PART_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")

logger = logging.getLogger(__name__)


class PinnedSafeNetworkBackend:
    """httpcore backend that connects only to a validated DNS result.

    httpcore performs TLS after ``connect_tcp`` using the original request
    hostname, so replacing only the TCP destination preserves Host/SNI while
    removing the validation-to-connect DNS rebinding window.
    """

    def __init__(self, *, allow_private: bool = False) -> None:
        from httpcore._backends.auto import AutoBackend

        self._backend = AutoBackend()
        self._allow_private = allow_private

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object | None = None,
    ) -> object:
        addresses = await asyncio.to_thread(_resolve_host_addresses, host, port)
        if not self._allow_private:
            for address in addresses:
                _raise_if_blocked_ip(address)
        elif addresses:
            _audit_private_egress_override("resolved_host", host)
        if not addresses:
            raise EgressBlockedError("Egress hostname did not resolve")
        destination = str(sorted(addresses, key=lambda item: (item.version, int(item)))[0])
        return await self._backend.connect_tcp(
            destination,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object | None = None,
    ) -> object:
        raise EgressBlockedError("Unix-socket egress is not allowed for source connectors")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def build_safe_async_http_transport(
    *,
    allow_private: bool = False,
    limits: object | None = None,
    verify: object = True,
    retries: int = 0,
) -> object:
    """Build an HTTPX transport with a DNS-pinned safe TCP backend."""
    import httpx

    kwargs: dict[str, object] = {
        "verify": verify,
        "trust_env": False,
        "retries": retries,
    }
    if limits is not None:
        kwargs["limits"] = limits
    transport = httpx.AsyncHTTPTransport(**kwargs)
    # HTTPX has no public network-backend hook. httpcore does; replacing this
    # one constructor-owned field keeps the standard HTTPX TLS/HTTP machinery.
    transport._pool._network_backend = PinnedSafeNetworkBackend(  # type: ignore[attr-defined]
        allow_private=allow_private
    )
    return transport


def validate_http_egress_url(url: str, *, allow_private: bool = False) -> None:
    """Validate that an HTTP(S) URL is safe to contact."""

    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"}:
        raise EgressBlockedError("Only http and https egress URLs are allowed")
    host = parsed.hostname
    if not host:
        raise EgressBlockedError("Egress URL must include a hostname")
    if allow_private:
        _audit_private_egress_override("http_url", host)
        return
    host = _normalize_host(host)
    if host.lower() in _LOCAL_HOSTNAMES or host.lower() in _METADATA_HOSTNAMES:
        raise EgressBlockedError("Private or local egress target is blocked")
    validate_host_egress(host, port=parsed.port, allow_private=allow_private)


def join_and_validate_http_egress_url(
    base_url: str,
    path: str,
    *,
    allow_private: bool = False,
) -> str:
    """Join a configured base URL and relative path, then validate egress safety.

    Connector/runtime callers should use this instead of string concatenation so
    path traversal or absolute-path tricks cannot silently change the upstream
    authority before the egress guard runs.
    """

    base = str(base_url or "").rstrip("/") + "/"
    raw_path = str(path or "")
    if "://" in raw_path:
        raise EgressBlockedError("Connector paths must be relative")

    decoded_path = unquote(raw_path)
    if "\\" in decoded_path:
        raise EgressBlockedError("Connector paths must not contain backslashes")
    if any(part == ".." for part in decoded_path.split("/")):
        raise EgressBlockedError("Connector paths must not escape the configured base URL")

    # quote(..., safe=...) preserves normal connector paths and query strings
    # while ensuring urljoin cannot reinterpret encoded control characters.
    joined = urljoin(base, quote(decoded_path.lstrip("/"), safe="/:?=&,%+@~-"))
    parsed_base = urlparse(base)
    parsed_joined = urlparse(joined)
    if (
        parsed_joined.scheme != parsed_base.scheme
        or parsed_joined.netloc.lower() != parsed_base.netloc.lower()
    ):
        raise EgressBlockedError("Connector path escaped the configured base URL")

    validate_http_egress_url(joined, allow_private=allow_private)
    return joined


def validate_host_egress(
    host: str,
    *,
    port: int | None = None,
    allow_private: bool = False,
) -> None:
    """Validate that a bare hostname/IP is safe to contact."""

    if not host:
        raise EgressBlockedError("Egress target must include a hostname")
    if allow_private:
        _audit_private_egress_override("host", host)
        return
    host = _normalize_host(host)
    if host.lower() in _LOCAL_HOSTNAMES or host.lower() in _METADATA_HOSTNAMES:
        raise EgressBlockedError("Private or local egress target is blocked")
    literal = _parse_host_ip(host)
    if literal is not None:
        _raise_if_blocked_ip(literal)
        return
    resolved = _resolve_host_addresses(host, port)
    if not resolved:
        raise EgressBlockedError("Egress hostname did not resolve")
    for ip in resolved:
        _raise_if_blocked_ip(ip)


def _resolve_host_addresses(
    host: str,
    port: int | None,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    literal = _parse_host_ip(_normalize_host(host))
    if literal is not None:
        return {literal}
    try:
        return {
            ipaddress.ip_address(result[4][0])
            for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise EgressBlockedError("Egress hostname could not be resolved safely") from exc


def _parse_host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    lowered = host.lower()
    dotted = _parse_dotted_ipv4_literal(lowered)
    if dotted is not None:
        return dotted
    if "." in lowered or ":" in lowered:
        return None
    try:
        if lowered.startswith("0x"):
            value = int(lowered, 16)
        elif lowered.startswith("0") and lowered != "0" and lowered.isdigit():
            value = int(lowered, 8)
        elif lowered.isdigit():
            value = int(lowered, 10)
        else:
            return None
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _parse_dotted_ipv4_literal(host: str) -> ipaddress.IPv4Address | None:
    parts = host.split(".")
    if len(parts) < 2 or len(parts) > 4:
        return None
    if not all(_NUMERIC_IPV4_PART_RE.match(part or "") for part in parts):
        return None
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


def _raise_if_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise EgressBlockedError("Private or local egress target is blocked")


def _normalize_host(host: str) -> str:
    return unquote(str(host).strip()).strip("[]")


def _audit_private_egress_override(kind: str, target: object) -> None:
    logger.warning(
        "allow_private_egress override accepted for %s target=%s",
        kind,
        target,
    )
