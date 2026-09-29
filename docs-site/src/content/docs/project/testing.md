---
title: Testing
description: The test suites, what each proves, and the gates a change must pass.
sidebar:
  order: 4
---

| Suite | Command | Needs |
|---|---|---|
| Unit | `make test-unit` | nothing |
| Dashboard | `uv run pytest tests/dashboard` | nothing |
| Integration | `make test-integration` | PostgreSQL and Redis for some tests |
| End-to-end | `make test-e2e` (stack running) or `make e2e` | Docker |
| Browser | `make test-browser` | Docker, Playwright's Chromium (`make browser-install`) |
| MCP SDK compatibility | `make test-mcp-sdk` | the `mcp-certification` extra |
| Load | `make load` | nothing; latency budgets, run separately |
| Live | `make live-certify` | real credentials; never in CI |

CI runs lint, types, the unit, dashboard and integration suites, security and
supply-chain checks, Helm rendering, the docs site build, and, on labelled pull
requests and on `main`, the end-to-end suite.

## Proving a test detects its defect

A test that passes when the control it guards is removed is not coverage.
`tools/audit/mutate.py` applies a named mutation to the source, such as
skipping a check, runs the tests that guard it, and reports whether they
caught it:

```bash
uv run python tools/audit/mutate.py --run mcp-cache-before-registry-check
make audit-mutations   # all of them
```

Add a mutation for every new governance control.

## Before release

`make final-boss-local` runs every local gate in order, including a Docker
build, the seeded end-to-end suite twice-seeded, the browser suite and the
mutations. It needs no live credentials.
