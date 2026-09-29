---
title: Evaluate with the seeded stack
description: Run the full local stack with seeded sources, identities and policies, and walk through it.
sidebar:
  order: 90
---

The seeded stack is what the project's own tests run against: every service,
plus local stand-ins for PostgreSQL, MySQL, an HTTP API, S3-compatible storage,
OpenSearch, Qdrant and mocked SaaS APIs, with sources, roles, identities,
policies, approvals and discovery content already in place. It needs Docker,
`uv` and Python 3.12.

```bash
make e2e-up
make e2e-seed
```

| | |
|---|---|
| Admin | `http://127.0.0.1:9090`, user `admin`, password `e2e-admin-password` |
| Agent API key | `ag-e2e-api-key` |
| Agent PostgreSQL login | `e2e_pg_user` / `e2e-pg-password` |

These credentials exist only in this local stack.
Live credentials are intentionally not required: nothing here reaches a real
external system.

:::caution
The seed writes directly to the control database. It skips configuration
validation, admin role checks and the admin audit log, and stores upstream
credentials inline. Use it to see a configured system, never as a model for a
real deployment; the [setup walkthrough](/get-started/setup-walkthrough/) is
that model.
:::

## Walk through it

1. **Overview**: health of the gateway, admin and workers.
2. **Data Sources**: the seeded sources. Open one and use **Test Connection**.
3. Its **Source Roles**: the scoped statements each role grants.
4. **Identities**: the seeded agent and its grants.
5. **Policies**: run a dry-run for a request that is allowed and one that is
   denied.
6. **Discovery**: search for seeded documents.
7. **Audit & Costs**: every request with its identity, source, role and policy
   decisions, cache and redaction.
8. **Write Safety**: queued writes and their outcomes.

## Run the tests

```bash
make test-e2e        # the end-to-end suite against the running stack
make e2e             # clean up, start, seed and run the suite, then stop
make final-boss-local  # the full local release gate; no live credentials
make e2e-down        # stop and delete the volumes
```

## If something is off

| Problem | Fix |
|---|---|
| A service is unhealthy | `make e2e-logs` |
| Admin sign-in fails | The admin is created at the first start from the compose file's bootstrap password. An older volume keeps its old admin: `make e2e-down`, then `make e2e-up`. |
| Data looks stale | `make e2e-seed` again; it is idempotent. |
| Ports are in use | Stop the other process, or change the port mappings. |
