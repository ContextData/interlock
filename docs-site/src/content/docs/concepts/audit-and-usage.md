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

Rows are written in batches, so a request can take a few seconds to appear. If
an export or the page shows nothing yet, refresh.

Read the log under **Audit & Costs**: filter by identity, source, protocol or
outcome, open any event for its full decision record, or export CSV. The
export includes the protocol and the request's correlation ID, the same ID
the event page shows. Deleted identities are still named.

### What a row's status means

A row records what InterLock did with the request: `success` means the
gateway answered it, `denied` that a source role or policy refused it, and
`error` that the source or the gateway failed. It cannot see what the client
then did with the answer.

### Redaction on a cache hit

A cached answer is stored after redaction, so serving it again masks the same
values. The PII fields of a cache-hit row describe the redaction that was
applied when the answer was cached; the event page says so. No scanner runs
on the hit itself.

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
