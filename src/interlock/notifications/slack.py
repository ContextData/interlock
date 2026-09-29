"""Slack transports.

Built on `httpx`, which is a core dependency, rather than `slack-sdk`, which
lives in the `connectors-tier1` extra and is only used for reading. A
notification path must work in every deployment profile.

Neither sender ever puts its credential in a log line, an exception message or
a repr. For the webhook transport the URL *is* the credential, so httpx errors
- which include the request URL - are caught and re-raised without it.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from interlock.security.egress import build_safe_async_http_transport, validate_http_egress_url


class SlackDeliveryError(Exception):
    """Delivery failed. Carries a reason, never a credential."""


class SlackSender(Protocol):
    async def send(self, payload: dict[str, Any]) -> None: ...


def _client(
    *, allow_private: bool, timeout: float, client: httpx.AsyncClient | None
) -> tuple[httpx.AsyncClient, bool]:
    if client is not None:
        return client, False
    return (
        httpx.AsyncClient(
            transport=build_safe_async_http_transport(allow_private=allow_private),
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        ),
        True,
    )


class SlackWebhookSender:
    """Post to an incoming-webhook URL."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 5.0,
        allow_private_egress: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        validate_http_egress_url(url, allow_private=allow_private_egress)
        self._url = url
        self._timeout = timeout
        self._allow_private = allow_private_egress
        self._client = client

    def __repr__(self) -> str:  # pragma: no cover - trivial, but the URL is a secret
        return "SlackWebhookSender(url=[REDACTED])"

    async def send(self, payload: dict[str, Any]) -> None:
        client, owned = _client(
            allow_private=self._allow_private, timeout=self._timeout, client=self._client
        )
        try:
            response = await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            # str(exc) embeds the request URL, which is the credential here.
            raise SlackDeliveryError(f"webhook request failed: {type(exc).__name__}") from None
        finally:
            if owned:
                await client.aclose()
        if response.status_code >= 300:
            raise SlackDeliveryError(f"webhook returned HTTP {response.status_code}")


class SlackBotSender:
    """Post to a channel with a bot token via `chat.postMessage`."""

    def __init__(
        self,
        token: str,
        channel: str,
        *,
        api_base_url: str = "https://slack.com/api",
        timeout: float = 5.0,
        allow_private_egress: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"{api_base_url.rstrip('/')}/chat.postMessage"
        validate_http_egress_url(self._url, allow_private=allow_private_egress)
        self._token = token
        self._channel = channel
        self._timeout = timeout
        self._allow_private = allow_private_egress
        self._client = client

    def __repr__(self) -> str:  # pragma: no cover - trivial, but the token is a secret
        return f"SlackBotSender(channel={self._channel!r}, token=[REDACTED])"

    async def send(self, payload: dict[str, Any]) -> None:
        body = {**payload, "channel": self._channel}
        client, owned = _client(
            allow_private=self._allow_private, timeout=self._timeout, client=self._client
        )
        try:
            response = await client.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise SlackDeliveryError(f"chat.postMessage failed: {type(exc).__name__}") from None
        finally:
            if owned:
                await client.aclose()
        if response.status_code >= 300:
            raise SlackDeliveryError(f"chat.postMessage returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise SlackDeliveryError("chat.postMessage returned a non-JSON body") from None
        if not data.get("ok"):
            # Slack's own error code, e.g. channel_not_found - safe to log.
            raise SlackDeliveryError(f"chat.postMessage rejected: {data.get('error')}")
