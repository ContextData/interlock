"""Render an approval event as a Slack message.

Pure functions: no I/O, no credentials, so the payload can be asserted on
directly in tests. What must never appear here is a raw statement, a request
body, or anything from `request_metadata` - the event already carries a
fingerprint, and everything else is left behind on purpose.
"""

from __future__ import annotations

from typing import Any

from interlock.notifications.events import ApprovalEvent

_HEADLINES = {
    "pending": "Write approval pending",
    "approved": "Write approved",
    "rejected": "Write rejected",
    "expired": "Write approval expired",
    "failed": "Approved write failed to execute",
}

_MAX_HEADER_CHARS = 150


def _summary(event: ApprovalEvent) -> str:
    who = event.identity_name or f"identity {event.identity_id}"
    operation = event.operation or "write"
    headline = _HEADLINES.get(event.kind, "Write approval update")
    text = f"InterLock: approval #{event.approval_id} - {headline.lower()}: "
    text += f"{event.risk_level}-risk {operation} on {event.source_id} by {who}"
    return text[:_MAX_HEADER_CHARS]


def build_slack_payload(event: ApprovalEvent, *, admin_base_url: str | None) -> dict[str, Any]:
    """Build the Block Kit payload, with a plain-text fallback.

    The action is a link to the Admin approval page, not an interactive
    button. Interactive buttons need a public inbound endpoint with Slack
    signature verification, replay protection, and a mapping from Slack user
    to InterLock admin RBAC - a new authenticated surface, and a larger
    decision than a notification.
    """
    fields = [
        f"*Approval*\n`#{event.approval_id}`",
        f"*Risk*\n`{event.risk_level}`",
        f"*Source*\n`{event.source_id}`",
        f"*Identity*\n{event.identity_name or f'#{event.identity_id}'}",
    ]
    if event.operation:
        via = f" via `{event.protocol}`" if event.protocol else ""
        fields.append(f"*Operation*\n`{event.operation}`{via}")
    if event.kind == "pending" and event.expires_at is not None:
        fields.append(f"*Expires*\n{event.expires_at.isoformat()}")
    if event.actor:
        fields.append(f"*Actor*\n{event.actor}")
    if event.kind == "approved":
        fields.append(f"*Executed*\n{'yes' if event.executed else 'no'}")
    if event.kind == "failed" and event.failure_kind:
        fields.append(f"*Failure*\n`{event.failure_kind}`")

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _HEADLINES.get(event.kind, "Write approval update"),
            },
        },
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": field} for field in fields[:10]],
        },
    ]
    if event.statement_fingerprint:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{event.statement_fingerprint}```"},
            }
        )
    if admin_base_url:
        url = f"{admin_base_url.rstrip('/')}/dashboard/write-safety/{event.approval_id}"
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Review in InterLock"},
                        "url": url,
                    }
                ],
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Approve or reject from the Admin console, or "
                        "`POST /api/approvals/<id>/approve|reject`. "
                        "Literal values are redacted from the statement above."
                    ),
                }
            ],
        }
    )
    return {"text": _summary(event), "blocks": blocks}
