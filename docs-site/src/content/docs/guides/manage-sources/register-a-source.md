---
title: Register a source
description: Add a database, bucket, workspace or API as a data source, from the console or the API.
sidebar:
  order: 1
---

## From the console

**Data Sources > New Source** opens a wizard:

1. **Type**: a display name and a connector. Only active connectors are
   listed. The source ID is generated from the name and shown on the review
   step; it cannot be changed later.
2. **Connection**: the connector's fields. Use **Test Connection** before
   going on; it probes with the values on screen and saves nothing.
3. **Schema** (PostgreSQL): lists tables and columns and flags likely PII by
   name. Ticked columns are recorded as PII in the catalog.
4. **Cache policy**: accept the connector's default or choose another.
5. **Review** and save. The source is registered, a catalog scan is queued,
   and every gateway reloads.

**Create default roles** is ticked by default and creates the connector's role templates; untick it to start with none.

## From the API

```bash
curl -sS -b cookies.txt -X POST "$ADMIN/api/data-sources" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"name": "Sales PostgreSQL", "source_type": "postgresql", "connector_key": "postgresql",
       "connection_config": {"host": "db.internal", "port": 5432, "database": "sales",
         "user": "interlock_ro", "password_ref": "env://SALES_DB_PASSWORD",
         "sslmode": "verify-full", "ssl_ca": "/run/secrets/sales-ca/ca.crt",
         "allow_private_egress": true}}'
```

The API validates the configuration strictly and returns the generated
`source_id`. `POST /api/data-sources/test` probes without saving. Signing in
to the API is covered in the [setup walkthrough](/get-started/setup-walkthrough/).

## Things to get right

- **Secrets as references.** Use `password_ref`, `token_ref` and so on, never
  literals, in anything but a local test.
- **Private addresses.** A host on a private network, such as a Docker
  Compose service or a VPC address, is refused unless the source opts in: tick
  **Allow a private network address** in the console (new source, wizard or
  edit page), or set `"allow_private_egress": true` through the API. Every use
  is logged.
- **Verified TLS in production** for PostgreSQL; see
  [Upstream TLS](/guides/manage-sources/upstream-tls/).
- **A least-privileged login.** The upstream login is the real limit on what
  InterLock can reach; give it no more than your roles describe.

Every connector's keys are on its page under [Connectors](/reference/connectors/).
