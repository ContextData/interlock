---
title: Write approval
description: How InterLock classifies writes by risk and holds risky ones for a person to review.
sidebar:
  order: 12
---

An agent that may write can still be held to a person's judgement for writes
that could do damage. InterLock classifies every write by risk and holds the
risky ones in an approval queue.

## Risk

| Risk | SQL | HTTP |
|---|---|---|
| Low | `INSERT` into a table that is not marked critical | `POST` |
| Medium | `UPDATE` or `DELETE` with a `WHERE` clause | `PUT`, `PATCH` |
| High | `UPDATE` or `DELETE` without `WHERE`, DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`), permission changes, `COPY`, `MERGE`, `SELECT INTO`, writes to critical tables, anything the parser cannot read | `DELETE` |
| Blocked | `EXECUTE`, `DO` and other dynamic execution | |

Source roles must still allow the write; risk only decides what happens to an
allowed one. A policy's `write_risk_cap` refuses anything above a level instead
of queueing it.

## What happens

- **Low** runs straight away.
- **Medium** and **high** are queued, not run. The agent is told the write was
  queued, with its approval id, and that it must not retry: over MCP the tool
  result says so with `isError: false`; the PostgreSQL wire refuses the
  statement with the id in the message; HTTP answers `202`.
- A reviewer (`approval_reviewer` or `security_admin`) sees it under **Write
  Safety** with the statement, the identity, the source and the risk, and
  approves or rejects it. Approving executes exactly once: it runs the stored
  statement, clears affected cache entries, and records the result, including
  failure.
- An approval nobody decides expires, after 15 minutes by default
  (`approvals.expiry_seconds`), and can then never run.

Everything is audited: the queued request, the decision, who made it, and the
execution.

## Notifications

With Slack notifications enabled, a message goes out when a write is queued
and when it is approved, rejected, expired or fails, linking to its page. The
statement in a message has literals replaced, so values do not leave in chat.
See [Slack approval notifications](/guides/day-to-day/slack-approval-notifications/).

## Where writes are not available

Object storage (S3 and Spaces) has no reachable governed write in this version:
an approved write fails. Slack and Google Workspace have no write path at all.
