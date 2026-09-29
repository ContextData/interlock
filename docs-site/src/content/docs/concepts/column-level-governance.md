---
title: Column-level governance
description: How InterLock enforces rules on columns for SQL sources, and where that stops.
sidebar:
  order: 11
---

For SQL sources InterLock resolves each column a query touches and checks it
against source roles and policy. A role can allow `sales.customers.name`
without allowing `sales.customers.ssn`, and a deny on a column is enforced on
agent paths.

## What counts as touching a column

Every column a statement reads or writes counts: the select list, `WHERE`,
`JOIN`, `GROUP BY`, `ORDER BY`, `UPDATE SET` and `INSERT` lists. `SELECT *` is
expanded to the table's columns from the catalog. Column names are resolved to
their tables the way the database would, using each source's default schema
and search path.

## When InterLock refuses rather than guesses

Where any statement that applies to a table is column-scoped, InterLock needs
to know exactly which columns a query touches. It **refuses** a query on that
table when it cannot tell:

- `SELECT *` on a table the catalog has not scanned yet;
- a whole-row reference such as `to_jsonb(c)`;
- a column it cannot resolve.

The refusal names the table. `COUNT(*)` needs table-level access for a
column-scoped role.

Roles that only use table or schema wildcards never trigger this.

## Redaction by column

A policy's `redact_columns` follows output columns back to their source, so
`SELECT ssn AS tax_id` is still redacted as `ssn`. This applies to MCP query
results and HTTP JSON bodies; the PostgreSQL wire applies only the PII scanner.

## Limits

Column rules are enforced by parsing the SQL the agent sends. A view, a
function, a trigger or dynamic SQL inside the database can read columns the
statement never names. The database login InterLock uses is the real
guarantee; see [Security model](/concepts/security-model/).
