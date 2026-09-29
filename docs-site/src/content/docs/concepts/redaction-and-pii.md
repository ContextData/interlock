---
title: Redaction and PII
description: How InterLock finds and redacts sensitive values in responses.
sidebar:
  order: 15
---

Responses are redacted before an agent sees them, in two ways.

## The PII scanner

Every response is scanned for values that look like personal data, whatever
column they are in, and each match is replaced with a marker such as
`[REDACTED:EMAIL]`. The built-in scanner recognises:

| Type | Example shape |
|---|---|
| `EMAIL` | `name@example.com` |
| `PHONE` | North American phone numbers |
| `SSN` | `123-45-6789` |
| `CREDIT_CARD` | 16-digit card numbers |
| `IP_ADDRESS` | IPv4 addresses |
| `MRN` | `MRN` followed by 6 to 10 digits |

Columns whose names suggest free text (`notes`, `description`, `body` and so
on) are scanned as text. An optional deeper scanner, built on Presidio, finds
names and other entities; it needs the `pii` extra and `pii.deep_enabled`.

The scanner applies on every protocol, including the PostgreSQL wire, and to
discovery results. It works on shapes: a sensitive value that looks like
nothing in the table above is not redacted.

## Policy redaction

A policy rule can name columns to redact whatever they contain, with
`redact_columns`. It follows output columns back to their source, so an alias
does not escape it. It applies to MCP query results and HTTP JSON bodies. **It
does not apply on the PostgreSQL wire.**

## What is recorded

Each audit row records whether PII was found, which types, and how many values
each redaction removed, so you can see redaction working without seeing the
values.

## Choosing between redaction and denial

Redaction keeps a query useful while hiding values. When a column must never
leave at all, deny it with a source role or a policy `columns` condition: a
denial holds on every protocol and does not depend on recognising a value's
shape.
