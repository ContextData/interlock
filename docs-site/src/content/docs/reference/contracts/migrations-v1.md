---
title: "InterLock Database Migration V1 Contract"
description: "How database migrations are applied, verified and never edited in V1."
normative: true
sidebar:
  order: 8
---

Status: stable release-candidate operational contract.

## File And Ordering Contract

Migrations live in `migrations/` and are named:

```text
<zero-padded-version>_<description>.sql
```

They are discovered lexicographically, checksummed with SHA-256, and recorded
in `schema_migrations(version, name, checksum, applied_at)`.

Released migration files are immutable. Changing the checksum of an applied
migration is an operator-visible error. Fixes use a new forward migration.

## Apply Contract

The supported runner is:

```bash
python -m interlock.db.migrate
```

The runner:

1. Creates `schema_migrations` if needed.
2. Acquires one PostgreSQL advisory lock.
3. Detects and baselines recognized legacy schemas when enabled.
4. Applies each pending SQL file in its own transaction.
5. Records its checksum only after that transaction succeeds.
6. Releases the advisory lock.

Running the same migration set repeatedly is idempotent. It does not mean each
arbitrary SQL statement is independently idempotent outside the runner.

## Readiness Contract

Serving processes verify that every bundled migration is recorded with the
expected checksum. They do not mutate schema during readiness. Deployment
automation must run the migration role before routing traffic to new code.

## Compatibility And Rollback

- Migrations are forward-only; V1 does not ship automatic down migrations.
- Expand/contract changes SHOULD span releases: add compatible schema first,
  migrate data, switch readers/writers, then remove old schema in a later minor
  release after deprecation.
- A release MUST document whether its migration remains readable by the prior
  application version.
- Rollback after an incompatible migration requires restoring the control
  database from a verified pre-upgrade backup.
- Destructive or long-running data migrations require a maintenance plan,
  estimated lock impact, and tested recovery procedure.

## Operator Requirements

Before upgrade:

- Verify a recent restorable PostgreSQL backup.
- Record current application/chart version and image digest.
- Run migration validation against a restored non-production copy.
- Confirm sufficient storage for tables, indexes, audit partitions, and WAL.

After upgrade:

- Verify migration head through `/ready`.
- Check audit partition and buffer health.
- Execute read, denied, and approval-gated smoke paths.
- Retain the pre-upgrade backup until the rollback window closes.

See [backup-restore.md](/operations/runbooks/backup-restore/) and
[upgrade-rollback.md](/operations/runbooks/upgrade-rollback/).
