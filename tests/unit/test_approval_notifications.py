"""Approval notifications: what leaves the system, and what cannot break.

Two properties matter more than the feature itself.

A notification leaves InterLock and lands in a Slack channel, so it must carry
no statement literal, no request metadata, and no credential - a webhook URL
is itself a credential, which is why even exception text is checked.

And delivery must never affect the request that triggered it. The governance
decision is already made and recorded by the time a notification is attempted;
a Slack outage must not become an agent-visible failure or a slower request.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from interlock.config import ApprovalConfig, InterLockConfig, NotificationConfig
from interlock.errors import ConfigValidationError
from interlock.notifications.events import ApprovalEvent, event_from_row
from interlock.notifications.factory import build_approval_notifier
from interlock.notifications.messages import build_slack_payload
from interlock.notifications.service import ApprovalNotifier
from interlock.notifications.slack import (
    SlackBotSender,
    SlackDeliveryError,
    SlackWebhookSender,
)
from interlock.security.egress import EgressBlockedError

SECRET_SQL = (
    "UPDATE customers SET email = 'ada@example.com', ssn = '123-45-6789' "
    "WHERE id = 987654 AND team = 'platform'"
)


def _event(**overrides: Any) -> ApprovalEvent:
    row = {
        "id": 42,
        "source_id": "warehouse",
        "identity_id": 7,
        "risk_level": "medium",
        "sql_text": SECRET_SQL,
        "request_metadata": {
            "identity_name": "analyst-claude",
            "protocol": "mcp",
            "normalized_operation": "UPDATE",
            "authorization": "Bearer super-secret-key",
            "body": {"data": {"ssn": "123-45-6789"}},
        },
    }
    event = event_from_row("pending", row, expires_at=datetime(2026, 9, 9, tzinfo=UTC))
    if overrides:
        return ApprovalEvent(**{**event.__dict__, **overrides})
    return event


class TestPayloadCarriesNothingSensitive:
    def test_no_statement_literal_or_identifier_survives(self) -> None:
        payload = build_slack_payload(_event(), admin_base_url="https://admin.example.com")
        rendered = str(payload)

        for leaked in ("ada@example.com", "123-45-6789", "987654", "super-secret-key"):
            assert leaked not in rendered, f"{leaked!r} reached the outbound payload"

    def test_the_fingerprint_still_conveys_the_shape_of_the_statement(self) -> None:
        """Redaction has to leave something a reviewer can act on."""
        rendered = str(build_slack_payload(_event(), admin_base_url=None))

        assert "UPDATE customers" in rendered
        assert "#42" in rendered
        assert "warehouse" in rendered
        assert "analyst-claude" in rendered

    def test_request_metadata_never_appears(self) -> None:
        """The event deliberately carries no metadata beyond named fields."""
        rendered = str(build_slack_payload(_event(), admin_base_url=None))

        assert "body" not in rendered
        assert "Bearer" not in rendered

    def test_the_link_button_appears_only_with_an_admin_base_url(self) -> None:
        with_url = build_slack_payload(_event(), admin_base_url="https://admin.example.com/")
        without = build_slack_payload(_event(), admin_base_url=None)

        actions = [block for block in with_url["blocks"] if block["type"] == "actions"]
        assert actions[0]["elements"][0]["url"] == (
            "https://admin.example.com/dashboard/write-safety/42"
        )
        assert not [block for block in without["blocks"] if block["type"] == "actions"]

    def test_a_long_statement_is_truncated(self) -> None:
        event = _event(statement_fingerprint="x" * 5000)
        rendered = str(build_slack_payload(event, admin_base_url=None))

        assert len(rendered) < 8000
        assert len(build_slack_payload(event, admin_base_url=None)["text"]) <= 150


class TestSenders:
    @pytest.mark.asyncio
    async def test_the_webhook_sender_posts_the_payload(self) -> None:
        seen: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.content
            return httpx.Response(200, text="ok")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # A loopback target with the private-egress escape hatch: the URL is
        # never contacted (MockTransport intercepts) but the constructor's
        # egress validation resolves the host, and a made-up hostname would
        # make this test depend on DNS.
        sender = SlackWebhookSender(
            "http://127.0.0.1:9/T/B/XYZ", allow_private_egress=True, client=client
        )

        await sender.send({"text": "hello"})

        assert seen["url"] == "http://127.0.0.1:9/T/B/XYZ"
        assert b"hello" in seen["body"]

    @pytest.mark.asyncio
    async def test_the_webhook_sender_raises_on_a_non_2xx(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        sender = SlackWebhookSender(
            "http://127.0.0.1:9/T/B/XYZ", allow_private_egress=True, client=client
        )

        with pytest.raises(SlackDeliveryError):
            await sender.send({"text": "hello"})

    @pytest.mark.asyncio
    async def test_the_bot_sender_addresses_a_channel_with_a_bearer_token(self) -> None:
        seen: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.content
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"ok": True, "ts": "1"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sender = SlackBotSender(
            "xoxb-test-token",
            "#approvals",
            api_base_url="http://127.0.0.1:9/api",
            allow_private_egress=True,
            client=client,
        )

        await sender.send({"text": "hello"})

        assert seen["auth"] == "Bearer xoxb-test-token"
        assert b"#approvals" in seen["body"]
        assert seen["url"].endswith("/chat.postMessage")

    @pytest.mark.asyncio
    async def test_the_bot_sender_raises_when_slack_reports_not_ok(self) -> None:
        """Slack answers HTTP 200 with `ok: false`; a status check alone misses it."""
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
            )
        )
        sender = SlackBotSender(
            "xoxb-test-token",
            "#nope",
            api_base_url="http://127.0.0.1:9/api",
            allow_private_egress=True,
            client=client,
        )

        with pytest.raises(SlackDeliveryError, match="channel_not_found"):
            await sender.send({"text": "hello"})

    @pytest.mark.asyncio
    async def test_a_transport_failure_never_echoes_the_webhook_url(self) -> None:
        """For a webhook, the URL is the credential."""
        secret_url = "http://127.0.0.1:9/T/B/SUPERSECRETTOKEN"

        async def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("failed to connect", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        sender = SlackWebhookSender(secret_url, allow_private_egress=True, client=client)

        with pytest.raises(SlackDeliveryError) as excinfo:
            await sender.send({"text": "hello"})

        assert "SUPERSECRETTOKEN" not in str(excinfo.value)
        assert "SUPERSECRETTOKEN" not in repr(sender)

    def test_the_bot_sender_repr_hides_its_token(self) -> None:
        sender = SlackBotSender(
            "xoxb-test-token",
            "#c",
            api_base_url="http://127.0.0.1:9/api",
            allow_private_egress=True,
        )
        assert "xoxb-test-token" not in repr(sender)


class _RecordingSender:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_times = fail_times

    async def send(self, payload: dict[str, Any]) -> None:
        self.calls.append(payload)
        if len(self.calls) <= self._fail_times:
            raise SlackDeliveryError("transient")


ALL_EVENTS = ("pending", "approved", "rejected", "expired", "failed")


class TestDeliveryIsIsolated:
    @staticmethod
    def _notifier(sender: Any, **kwargs: Any) -> ApprovalNotifier:
        return ApprovalNotifier(
            sender, events=ALL_EVENTS, timeout_seconds=1.0, max_attempts=3, **kwargs
        )

    @pytest.mark.asyncio
    async def test_notify_returns_before_delivery_and_then_delivers(self) -> None:
        sender = _RecordingSender()
        notifier = self._notifier(sender)

        notifier.notify(_event())
        assert sender.calls == []  # nothing awaited yet

        await notifier.aclose()
        assert len(sender.calls) == 1

    @pytest.mark.asyncio
    async def test_a_sender_that_always_raises_never_propagates(self) -> None:
        class Boom:
            async def send(self, payload: dict[str, Any]) -> None:
                raise RuntimeError("slack is down")

        notifier = self._notifier(Boom())

        notifier.notify(_event())
        await notifier.aclose()  # must not raise

    @pytest.mark.asyncio
    async def test_retries_are_bounded(self) -> None:
        sender = _RecordingSender(fail_times=99)
        notifier = ApprovalNotifier(sender, events=ALL_EVENTS, timeout_seconds=1.0, max_attempts=2)

        notifier.notify(_event())
        await asyncio.sleep(0.7)
        await notifier.aclose()

        assert len(sender.calls) == 2

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_retried_and_then_succeeds(self) -> None:
        sender = _RecordingSender(fail_times=1)
        notifier = self._notifier(sender)

        notifier.notify(_event())
        await asyncio.sleep(0.7)
        await notifier.aclose()

        assert len(sender.calls) == 2

    @pytest.mark.asyncio
    async def test_event_kinds_outside_the_configured_set_are_dropped(self) -> None:
        sender = _RecordingSender()
        notifier = ApprovalNotifier(sender, events=("approved",), timeout_seconds=1.0)

        notifier.notify(_event())
        await notifier.aclose()

        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_failures_are_logged_without_the_payload(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Boom:
            async def send(self, payload: dict[str, Any]) -> None:
                raise RuntimeError(f"leaky error containing {SECRET_SQL}")

        notifier = ApprovalNotifier(Boom(), events=ALL_EVENTS, timeout_seconds=1.0, max_attempts=1)

        with caplog.at_level(logging.WARNING):
            notifier.notify(_event())
            await notifier.aclose()

        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "ada@example.com" not in text
        assert "123-45-6789" not in text
        assert "RuntimeError" in text


class TestFactory:
    @staticmethod
    def _config(**notification_kwargs: Any) -> InterLockConfig:
        config = InterLockConfig()
        config.notifications = NotificationConfig(**notification_kwargs)
        return config

    def test_disabled_returns_nothing(self) -> None:
        assert build_approval_notifier(self._config()) is None

    def test_a_bot_token_reference_is_resolved_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SLACK_TOKEN", "xoxb-resolved")
        notifier = build_approval_notifier(
            self._config(
                enabled=True,
                slack_bot_token_ref="env://TEST_SLACK_TOKEN",
                slack_channel="#approvals",
            )
        )

        assert notifier is not None
        assert "xoxb-resolved" not in repr(notifier)

    def test_a_missing_environment_variable_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Better a boot failure than a system that silently never notifies."""
        monkeypatch.delenv("TEST_SLACK_ABSENT", raising=False)

        with pytest.raises(ConfigValidationError, match="slack_bot_token_ref"):
            build_approval_notifier(
                self._config(
                    enabled=True,
                    slack_bot_token_ref="env://TEST_SLACK_ABSENT",
                    slack_channel="#approvals",
                )
            )

    def test_an_empty_secret_refuses_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SLACK_EMPTY", "")

        with pytest.raises(ConfigValidationError, match="empty"):
            build_approval_notifier(
                self._config(
                    enabled=True,
                    slack_bot_token_ref="env://TEST_SLACK_EMPTY",
                    slack_channel="#approvals",
                )
            )

    def test_a_bot_token_wins_over_a_webhook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SLACK_TOKEN", "xoxb-resolved")
        monkeypatch.setenv("TEST_SLACK_HOOK", "https://hooks.example.com/T/B/X")
        notifier = build_approval_notifier(
            self._config(
                enabled=True,
                slack_bot_token_ref="env://TEST_SLACK_TOKEN",
                slack_webhook_url_ref="env://TEST_SLACK_HOOK",
                slack_channel="#approvals",
            )
        )

        assert notifier is not None
        assert "SlackBotSender" in repr(notifier)

    def test_a_private_webhook_target_is_refused_unless_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SLACK_HOOK", "http://127.0.0.1:8088/slack/webhook")

        with pytest.raises(EgressBlockedError):
            build_approval_notifier(
                self._config(enabled=True, slack_webhook_url_ref="env://TEST_SLACK_HOOK")
            )

        notifier = build_approval_notifier(
            self._config(
                enabled=True,
                slack_webhook_url_ref="env://TEST_SLACK_HOOK",
                allow_private_egress=True,
            )
        )
        assert notifier is not None


class TestApprovalConfig:
    def test_the_expiry_default_is_unchanged(self) -> None:
        """900 seconds was hard-coded; the default must stay compatible."""
        assert ApprovalConfig().expiry_seconds == 900

    def test_the_expiry_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalConfig(expiry_seconds=5)
        assert ApprovalConfig(expiry_seconds=3600).expiry_seconds == 3600

    def test_notifications_enabled_requires_a_target(self) -> None:
        with pytest.raises(ValidationError, match="requires"):
            NotificationConfig(enabled=True)

    def test_a_bot_token_requires_a_channel(self) -> None:
        with pytest.raises(ValidationError, match="slack_channel"):
            NotificationConfig(enabled=True, slack_bot_token_ref="env://X")

    def test_a_literal_in_a_reference_field_is_rejected(self) -> None:
        """The commonest misconfiguration: pasting the token itself."""
        with pytest.raises(ValidationError, match="secret reference"):
            NotificationConfig(slack_bot_token_ref="xoxb-a-real-looking-token")
