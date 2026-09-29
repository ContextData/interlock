---
title: Evaluate InterLock
description: Two local tracks - run the quick start from empty and check it with make smoke-eval, or explore the fully seeded demo stack.
sidebar:
  order: 90
---

There are two ways to evaluate InterLock locally, and they answer different
questions:

| Track | Answers | Start |
|---|---|---|
| Start from empty | Can a new operator get from nothing to a governed, audited query? | The [quick start](/get-started/quick-start/), then `make smoke-eval` |
| Explore the seeded demo | What does a fully configured system look like? | `make e2e-up` and `make e2e-seed`, below |

## Start from empty

Follow the [quick start](/get-started/quick-start/) by hand first: it is the
path a new operator takes, and every step happens in the console.

`make smoke-eval` then runs the same journey as a script, against its own
Compose project (`interlock-smoke`, on ports 19090, 13001 and 15434, so it
does not collide with a stack you already have). It needs Docker and Python 3,
and nothing else: no model keys and no external accounts.

```bash
make smoke-eval
```

It signs in as `admin` / `admin` and changes the password, registers the
sample database through the wizard, allows reads by policy, creates an agent
with the `read` role, and then checks what the quick start promises:

- The same `SELECT` over PostgreSQL and MCP returns the same rows, with the
  email addresses redacted, whichever protocol asks first and whether or not
  the answer came from cache.
- A `DELETE` is refused over both protocols, and the `orders` table still has
  its 5 rows.
- The audit log has every request, with its protocol and outcome, and records
  the redaction on the cached answers too.

Each check prints `PASS` or `FAIL`, the result is written to
`build/smoke-eval/result.json`, and the command exits non-zero if any check
failed. The stack is deleted afterwards; add `--keep` to leave it running
(`python3 tools/smoke_eval.py --keep`). CI runs it on `main` and on labelled
pull requests.

A passing run shows that the documented first-run path works. It is not a
security, performance or release test: those are the suites on the
[testing](/project/testing/) page.

## Explore the seeded demo

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

### Walk through it

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

### Run the tests

```bash
make test-e2e        # the end-to-end suite against the running stack
make e2e             # clean up, start, seed and run the suite, then stop
make final-boss-local  # the full local release gate; no live credentials
make e2e-down        # stop and delete the volumes
```

### If something is off

| Problem | Fix |
|---|---|
| A service is unhealthy | `make e2e-logs` |
| Admin sign-in fails | The admin is created at the first start from the compose file's bootstrap password. An older volume keeps its old admin: `make e2e-down`, then `make e2e-up`. |
| Data looks stale | `make e2e-seed` again; it is idempotent. |
| Ports are in use | Stop the other process, or change the port mappings. |
