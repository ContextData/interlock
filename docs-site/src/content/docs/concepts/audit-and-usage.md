---
title: Audit and usage
description: What InterLock records about every request and admin action, and how to read it.
sidebar:
  order: 13
---

## The audit log

Every governed request writes one row to the audit log, whatever the outcome:
the identity, the source, the protocol, the statement or path, the tables it
touched, the source-role and policy decisions, whether it was served from
cache, whether PII was found and how much was redacted, the approval state for
a write, the status and the latency.

Rows are written in batches. If the database is briefly unavailable they are
retried, then spooled to disk and replayed. How strictly this is enforced is
`audit.durability_mode`:

| Mode | When an audit row cannot be kept |
|---|---|
| `strict` | the request is refused. Required in production. |
| `retriable` | the request proceeds; the row is spooled and replayed |
| `best_effort` | the row is dropped |

Read the log under **Audit & Costs**: filter by identity, source, protocol or
outcome, open any event for its full decision record, or export CSV. Deleted
identities are still named.

## The admin audit log

Admin actions are recorded separately: every change in the console or admin
API, sign-ins, password changes, connector activation, approvals and
rejections. Security-sensitive changes fail rather than proceed unrecorded.

## Usage

The usage breakdown counts requests by outcome (served upstream, served from
cache, denied, errors, queued) per identity, source and team, over 24 hours,
7 days or 30 days. These are counts, not costs: InterLock does not price
requests. Snowflake queries record the credits and bytes the warehouse reports,
shown on the event.
