---
title: PostgreSQL clients
description: Connect psql, a driver or an ORM to the gateway's PostgreSQL listener.
sidebar:
  order: 2
---

The gateway looks like a PostgreSQL server. Connect to it instead of the
database:

| Setting | Value |
|---|---|
| Host, port | the gateway's PostgreSQL listener, `5432` in the container (`5434` on the local Compose stack) |
| Database | the **source ID** |
| User | anything, when the password is an API key |
| Password | the identity's **API key** |
| TLS | required in production (`sslmode=require` or stronger) |

```bash
PGPASSWORD="$API_KEY" psql "host=$GATEWAY_HOST port=5432 user=agent dbname=sales_pg sslmode=require"
```

An identity can instead have a dedicated PostgreSQL username and password, set
on its page, for tools that cannot put a key in the password field.

The gateway never passes the agent's credentials upstream: it connects to the
source with the credentials registered for the source.

## What is supported

The simple and extended query protocols, prepared statements and
multi-statement queries (each statement is governed). `COPY`, cancelling
another session's query, and GSS encryption are not supported. asyncpg is the
certified client; others that stay within the protocol subset work, but are
not certified. See the [PostgreSQL contract](/reference/contracts/postgresql-v1/).

## Refusals

A refused statement returns an ordinary PostgreSQL error, so client code sees
it as a query error: `42501` for a source-role or policy denial with the
reason in the message, `3D000` for an unknown or withdrawn source, `08004` or
`08006` when the upstream cannot be used. A write held for approval is refused
with the approval id in the message.

Remember that on this protocol policy `redact_columns` does not apply; only the
PII scanner redacts.
