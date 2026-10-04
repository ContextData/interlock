---
title: Upgrades and migrations
description: Upgrade InterLock safely, and how database migrations are applied.
sidebar:
  order: 10
---

Each release lists its changes in `CHANGELOG.md`, with breaking changes marked.
Read it before upgrading.

## Migrations

Schema changes ship as numbered SQL files. `interlock-migrate`
(`python -m interlock.db.migrate`, and the Helm chart's pre-upgrade job) applies
those not yet recorded in the `schema_migrations` table, in order, inside a
lock so two runners cannot race. Each applied file's checksum is recorded, and
readiness fails if a bundled file no longer matches: migrations are never
edited after release. Services refuse to report ready until the database is at
the migration head they expect.

## Upgrading

1. Back up the control database.
2. With Helm, upgrade to the new chart and image digests; the migration job runs
   first, then the pods roll. With Compose, pull or build the new image and
   `docker compose up -d`; the migration service runs first.
3. Watch `/ready` on every service.

Rolling back after a migration needs the backup; migrations are forward-only.
The full procedure, including rollback, is the
[upgrade and rollback runbook](/operations/runbooks/upgrade-rollback/), and the
rules migrations follow are in the [migrations contract](/reference/contracts/migrations-v1/).
