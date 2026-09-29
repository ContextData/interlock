---
title: Development environment
description: Set up a machine to work on InterLock.
sidebar:
  order: 2
---

You need Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/), and Docker for
the end-to-end stack. The docs site needs Node 24.

```bash
uv sync --locked --extra dev
uv run pytest tests/unit -q
```

The `dev` extra is enough for most work; tests that need an optional extra skip
themselves. Install more only for what you are changing:

| Working on | Add |
|---|---|
| Deep PII scanning | `--extra pii` |
| Discovery embeddings | `--extra ml --extra vector` |
| A connector | `--extra connectors-tier1`, `--extra connectors-tier2` or `--extra connectors-repo` |
| Type checking (`make type`) | at least `--extra ingestion` |
| Everything CI runs | `make install` (several gigabytes) |

## Running the services

`make e2e-up && make e2e-seed` runs the full seeded stack in Docker; see
[Evaluate with the seeded stack](/guides/evaluate-with-the-seeded-stack/). To
run a service from source against it, start only the databases and use the
module entry points, for example `uv run python -m interlock.admin` with the
`INTERLOCK_*` settings pointing at `localhost:5433` and `localhost:6380`.

## Infrastructure tools

`make check-infra` needs `terraform`, `actionlint` and `helm`. On Apple Silicon
use an arm64 build of terraform (`terraform version` reports `darwin_arm64`);
an x86_64 build under Rosetta makes validation appear to hang.
