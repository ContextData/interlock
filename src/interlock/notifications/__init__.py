"""Outbound notifications for write approvals.

A write held for approval used to be visible only to someone already looking
at the Admin console. Nothing emitted an event, so the practical procedure was
for a human to poll - and the queue expired entries after fifteen minutes.

Delivery is deliberately best-effort and never blocks the agent request that
triggered it. A Slack outage must not turn into a governance outage.
"""

from interlock.notifications.events import ApprovalEvent, ApprovalEventKind
from interlock.notifications.factory import build_approval_notifier
from interlock.notifications.messages import build_slack_payload
from interlock.notifications.service import ApprovalNotifier, ApprovalNotifierProtocol
from interlock.notifications.slack import (
    SlackBotSender,
    SlackDeliveryError,
    SlackSender,
    SlackWebhookSender,
)

__all__ = [
    "ApprovalEvent",
    "ApprovalEventKind",
    "ApprovalNotifier",
    "ApprovalNotifierProtocol",
    "SlackBotSender",
    "SlackDeliveryError",
    "SlackSender",
    "SlackWebhookSender",
    "build_approval_notifier",
    "build_slack_payload",
]
