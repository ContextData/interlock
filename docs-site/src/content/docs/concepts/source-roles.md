---
title: Source roles
description: How source roles decide what an agent may do on a source.
sidebar:
  order: 8
---

A **source role** belongs to one source and lists what an agent holding it may
do there. Roles are the only thing that grants access: without a role that
allows a request, it is denied. Role permission does not bypass policy rules or
write safety: a request a role allows still goes through policy, and a risky
write still waits for approval.

## Statements

A role is a list of statements. Each statement has:

| Part | Meaning | Example |
|---|---|---|
| Effect | `allow` or `deny` | `allow` |
| Action | what is being done | `db.table.select`, `storage.object.get`, `collab.message.read` |
| Resource type | what kind of thing | `db.table`, `db.column`, `storage.object` |
| Resource pattern | which ones, with `*` wildcards | `sales.customers.*`, `s3://bucket/reports/*` |
| Conditions | optional narrowing | `{"risk_levels": ["low"]}` |

Each connector has its own vocabulary of actions and resource types; the role
editor offers only those, and refuses a statement that uses an action or
condition the connector does not have. [Role conditions](/reference/role-conditions/)
lists the conditions.

## How a request is decided

1. Every resource the request touches is collected: for SQL, each table and,
   where the catalog resolves them, each column.
2. If any `deny` statement in any held role matches, the request is denied.
   Explicit deny always wins.
3. Every resource must be matched by some `allow` statement. One unallowed
   resource denies the whole request.

A pattern's `*` matches across dots, so `sales.*` covers `sales.customers` and
every column in it. A `db.table` statement also covers requests for that
table's columns.

## Templates

Each connector ships role templates (for PostgreSQL: `read`, `analyst`,
`write`, `owner`, `blocked`). The console can create them when a source is
registered, and a new role starts from the connector's reader template. Edit
them to fit.

## SQL and non-SQL sources

For SQL sources (PostgreSQL, MySQL, Snowflake) patterns are enforced down to
columns. For object storage, Slack and GitHub, agents are gated by **action**;
resource patterns on those roles are evaluated in dry-runs but not yet on agent
paths, and the role editor says so.

## Deleting a role

A role cannot be deleted while any identity holds an active grant of it:
revoke the grants first. The deletion is recorded with the role's statements
and grant history. Policies that named the role key stop matching it.

See [Write source roles](/guides/control-access/write-source-roles/) for the
editor, and [Column-level governance](/concepts/column-level-governance/) for
column rules.
