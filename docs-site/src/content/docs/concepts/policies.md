---
title: Policies
description: How policy rules shape requests that source roles allowed.
sidebar:
  order: 10
---

A **policy rule** applies organisation-wide governance to requests that source
roles have already allowed. Policies restrict and shape; they **never grant**
access a role did not. Policies must not grant source access on their own, and
an `allow` rule is only a way of letting through, and shaping, what a role
already allows.

## How rules are chosen

Enabled rules are checked in priority order, highest first. The first rule
whose conditions all match decides. **A request no rule matches is denied**, so
a deployment needs at least one `allow` rule covering the traffic it expects.

## Conditions

All the conditions a rule sets must match:

| Condition | Matches when |
|---|---|
| `source_ids` | the request is on one of these sources |
| `operation_types` | it is a `read`, `write` or `discovery` |
| `roles` | the identity holds one of these source role keys on the source |
| `identity_roles` | the identity carries one of these legacy labels |
| `tables` | the request touches one of these tables |
| `columns` | the request touches one of these columns (deny rules) |
| `classifications` | the request carries one of these classification tags. No agent path sets classification tags today, so a rule that uses this condition does not match agent traffic; it is evaluated in dry-runs only |

A `deny` rule's `tables` condition matches every spelling of a table: bare,
schema-qualified, or the legacy `public.` form, so a query cannot slip past a
deny by writing the name differently.

## Actions

| Action | Effect |
|---|---|
| `effect: deny` | refuse the request, with the rule named in the reason |
| `effect: allow` | let it through, possibly shaped by the actions below |
| `redact_columns` | redact these columns in MCP results and HTTP JSON bodies (not on the PostgreSQL wire) |
| `rate_limit` | requests per minute for this traffic |
| `write_risk_cap` | the highest write risk allowed at all; anything above is refused rather than queued |

A source's page shows the rules that apply to it, in order, as sentences, with
how many requests each matched recently. Test a rule before relying on it with
the policy dry-run. See [Write policy rules](/guides/control-access/write-policy-rules/).
