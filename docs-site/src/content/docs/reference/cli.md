---
title: Command line
description: The commands the InterLock package installs, and what they read.
sidebar:
  order: 5
---

Installing `interlock-runtime` provides four commands. Each reads its
configuration from `INTERLOCK_*` environment variables and an optional YAML
file ([Configuration](/reference/configuration/)).

| Command | Runs | Same as |
|---|---|---|
| `interlock-gateway` | the gateway | `python -m interlock.gateway` |
| `interlock-admin` | the admin console and API | `python -m interlock.admin` |
| `interlock-worker` | a worker | `python -m interlock.worker` |
| `interlock-migrate` | applies database migrations, then exits | `python -m interlock.db.migrate` |

`interlock-migrate` connects with `DATABASE_URL`, or the
`INTERLOCK_DATABASE__*` settings, and applies every migration not yet
recorded, in order, verifying the checksum of each one already applied.

| Flag | Meaning |
|---|---|
| `--migrations-dir PATH` | where the `.sql` files are; defaults to the packaged ones |
| `--no-baseline-existing` | do not baseline a database created by an older, pre-ledger version |

The container image runs the same commands, for example
`docker run <image> interlock.gateway`. See
[Upgrades and migrations](/operations/upgrades-and-migrations/).
