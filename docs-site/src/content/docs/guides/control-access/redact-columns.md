---
title: Redact columns
description: Hide named columns in responses with a policy, and know where that applies.
sidebar:
  order: 6
---

To hide columns whatever their values look like, add an `allow` policy rule
with **Redact columns**, for example `ssn,email,phone`, scoped to the source.
Matching values are replaced in the response.

It applies to MCP query results and HTTP JSON bodies, and follows columns
through aliases (`SELECT ssn AS tax_id` is still redacted). **It does not
apply on the PostgreSQL wire**, where only the PII scanner redacts.

If a column must never leave on any protocol, deny it instead: a source role
`deny` statement on `schema.table.column`, or a policy `deny` rule with a
**Columns** condition. See [Redaction and PII](/concepts/redaction-and-pii/).
