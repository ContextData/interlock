---
title: Certify a new agent
description: The checks to run before handing an identity to a new agent or team.
sidebar:
  order: 5
---

Before sharing an identity, prove on a test source that each control behaves as
you expect for that agent:

- An allowed read succeeds.
- A source or table outside its roles is denied.
- A request a policy denies is denied even though a role allows it.
- A risky write is queued, or refused, as the policy says.
- A rejected or expired approval does not execute; an approved one executes
  exactly once.
- Sensitive values come back redacted.
- **Audit & Costs** shows each request with the identity, source, protocol, the
  source-role and policy decisions, the approval state, the cache decision and
  the redaction counts.

The policy and source-role dry-runs (on the Policies page and each source's
roles) answer "would this be allowed" without sending anything upstream.

Use disposable tables, prefixes, channels or repositories for these checks, and
never real credentials in reports or screenshots. Do not give agents direct
origin credentials for a governed source: an agent that can reach the source
itself can bypass every control here.
