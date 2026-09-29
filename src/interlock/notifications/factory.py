"""Build a notifier from configuration, resolving secret references.

A misconfiguration fails at startup rather than silently disabling
notifications: an operator who set `enabled: true` and mistyped the variable
name should get a boot error, not a system that quietly never notifies anyone.
"""

from __future__ import annotations

import logging

import httpx

from interlock.config import InterLockConfig
from interlock.errors import ConfigValidationError
from interlock.notifications.service import ApprovalNotifier
from interlock.notifications.slack import SlackBotSender, SlackSender, SlackWebhookSender
from interlock.secrets.resolver import MissingSecretError, resolve

logger = logging.getLogger(__name__)


def build_approval_notifier(
    config: InterLockConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> ApprovalNotifier | None:
    """Return a configured notifier, or None when notifications are disabled."""
    settings = config.notifications
    if not settings.enabled:
        return None

    def _resolve(field: str, reference: str) -> str:
        try:
            value = resolve(reference)
        except MissingSecretError as exc:
            raise ConfigValidationError(
                f"notifications.{field} could not be resolved: {exc}"
            ) from None
        if not value:
            # An empty secret would build a sender that authenticates as
            # nobody and fails on every send, which is harder to diagnose than
            # refusing to start.
            raise ConfigValidationError(f"notifications.{field} resolved to an empty value")
        return value

    sender: SlackSender
    if settings.slack_bot_token_ref:
        # Preferred over the webhook: channel-addressed, and the token is not
        # itself the destination, so it can be rotated without re-pointing.
        token = _resolve("slack_bot_token_ref", settings.slack_bot_token_ref)
        channel = settings.slack_channel or ""
        sender = SlackBotSender(
            token,
            channel,
            api_base_url=settings.slack_api_base_url,
            timeout=settings.timeout_seconds,
            allow_private_egress=settings.allow_private_egress,
            client=http_client,
        )
        logger.info("approval notifier enabled: mode=bot channel=%s", channel)
    else:
        if settings.slack_webhook_url_ref:
            url = _resolve("slack_webhook_url_ref", settings.slack_webhook_url_ref)
        else:
            url = settings.slack_webhook_url or ""
            logger.warning(
                "notifications.slack_webhook_url is deprecated; "
                "use slack_webhook_url_ref with a secret reference"
            )
        sender = SlackWebhookSender(
            url,
            timeout=settings.timeout_seconds,
            allow_private_egress=settings.allow_private_egress,
            client=http_client,
        )
        logger.info("approval notifier enabled: mode=webhook")

    return ApprovalNotifier(
        sender,
        events=settings.approval_events,
        timeout_seconds=settings.timeout_seconds,
        max_attempts=settings.max_attempts,
        admin_base_url=settings.admin_base_url,
    )
