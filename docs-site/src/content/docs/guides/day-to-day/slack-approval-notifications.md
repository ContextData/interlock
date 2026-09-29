---
title: Slack approval notifications
description: Post a Slack message when a write is queued, approved, rejected, expired or fails.
sidebar:
  order: 2
---

Approval notifications are beta. Configure them on the gateway and admin:

| Setting | Value |
|---|---|
| `notifications.enabled` | `true` |
| `notifications.slack_bot_token_ref` and `notifications.slack_channel` | a bot token as a secret reference, and the channel to post to (preferred) |
| `notifications.slack_webhook_url_ref` | or an incoming-webhook URL, as a secret reference |
| `notifications.admin_base_url` | the console's URL, so messages link to the approval |
| `notifications.approval_events` | which of `pending`, `approved`, `rejected`, `expired`, `failed` to send |

As environment variables these are `INTERLOCK_NOTIFICATIONS__ENABLED` and so
on. The bot needs `chat:write` and must be in the channel.

Each message names the approval, the source, the identity and the risk, with
the statement's literals replaced, and a link to its page. Delivery happens in
the background with bounded retries: a Slack outage never delays or fails an
agent's request, but a message can be lost if the gateway stops between queueing
and sending. The approval itself is always in **Write Safety**.
