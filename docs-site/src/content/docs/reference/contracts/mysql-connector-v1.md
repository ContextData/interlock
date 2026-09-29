---
title: "MySQL And MariaDB Connector V1 Contract"
description: "The MySQL and MariaDB connector behaviour promised in V1."
normative: true
sidebar:
  order: 9
---

Status: stable connector surface for `1.0.0-rc.1`.

## Important Boundary

InterLock V1 does not provide a MySQL wire-protocol listener. MySQL and
MariaDB are governed connector sources reached through InterLock MCP/Admin
workflows and the shared connector execution path. `supports_proxy` metadata
must not be interpreted as native MySQL client compatibility until a wire
listener is separately implemented and certified.

## Connection Semantics

- Async execution uses `aiomysql`.
- SQL parsing and normalization use the `sqlglot` MySQL dialect.
- Connection configuration includes host, port, database, user, password or
  password reference, and TLS settings where configured.
- Runtime source credentials are resolved by InterLock and are not returned to
  agents or Admin pages.
- Source probing validates egress policy before connection.

## Supported Operations

- Connection probe.
- `information_schema` database/table/column introspection.
- Single-statement SELECT reads.
- Role-scoped INSERT and UPDATE.
- Explicitly role-scoped DELETE and DDL, subject to write-safety approval.
- Discovery/catalog ingestion from schema metadata.

The connector returns row objects keyed by column name. Through MCP, those rows
use the MCP text-content JSON envelope.

## SQL Safety Contract

- A read path accepts exactly one read-only statement.
- Multi-statement input is rejected.
- `SELECT ... INTO OUTFILE`, `DUMPFILE`, session mutation, unsupported AST
  classes, and side-effecting forms fail closed.
- A write path accepts exactly one statement whose classified action is
  allowed by the identity's source role.
- INSERT, UPDATE, DELETE, TRUNCATE, and DDL are distinct permission actions.
- Parse failure cannot silently become a read. An explicit raw-execution
  permission, where supported, remains high risk and approval governed.

## Authorization And Write Safety

Database resources use `schema.table` and, where available,
`schema.table.column`. Default role intent is:

| Role | Intended permissions |
| --- | --- |
| `read` | List/describe/select only |
| `writer` | Read plus insert/update; no delete or DDL |
| `maintainer` | Read/write plus explicitly scoped row delete |
| `schema_owner` | Explicit DDL and destructive actions, approval governed |
| `blocked` | Explicit deny |

These are templates, not immutable business roles. Admins must review resource
patterns before assigning them. Explicit deny wins, role allows are unioned,
and no matching allow means deny.

## Transactions, Procedures, And Compatibility

InterLock V1 does not promise a persistent MySQL client session, arbitrary
session variables, stored procedure execution, binary protocol prepared
statements, LOAD DATA, replication, or MySQL wire compatibility. Such requests
must fail closed unless a later documented connector version adds them.

MariaDB uses the same V1 adapter contract. Provider-specific SQL differences
beyond the tested `sqlglot` MySQL subset are beta behavior.
