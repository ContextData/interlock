---
title: "Runbooks"
description: "The operational runbooks for running InterLock in production."
normative: true
sidebar:
  order: 0
---

These runbooks define the minimum operating procedures for an InterLock V1
deployment. They are provider-neutral. Commands that mutate production require
the operator's normal change-control and backup approval process.

## Runbook Index

| Runbook | Purpose |
| --- | --- |
| [service-operations.md](/operations/runbooks/service-operations/) | Daily checks, readiness, capacity, dependency failure behavior |
| [availability-rpo-rto.md](/operations/runbooks/availability-rpo-rto/) | Reference recovery objectives and architecture assumptions |
| [backup-restore.md](/operations/runbooks/backup-restore/) | Backup scope, verification, restore, and recovery validation |
| [upgrade-rollback.md](/operations/runbooks/upgrade-rollback/) | Preflight, migration, rollout, rollback, and compatibility checks |
| [incident-response.md](/operations/runbooks/incident-response/) | Detection, containment, evidence, communication, and recovery |
| [audit-retention.md](/operations/runbooks/audit-retention/) | Audit retention, immutability, partition maintenance, and disposal |

## Mandatory Rehearsals Before V1 Production

- Restore a control-database backup into an isolated environment.
- Replay an audit spool after a database outage.
- Recover from Redis loss and verify strict cache-write behavior.
- Kill one Gateway and one Worker during active traffic.
- Upgrade from the prior supported release candidate.
- Exercise application rollback with a compatible migrated schema.
- Exercise database restore when schema rollback is required.
- Verify incident contacts and security disclosure routing.

Record the date, release SHA, artifact digest, operator, duration, and outcome.
Never include credentials or unredacted production payloads in evidence.

## Responsibility Boundary

InterLock provides health checks, migration tooling, durable-audit mechanisms,
and reproducible deployment assets. The operator owns database/Redis service
class, backups, encryption keys, DNS/TLS, storage replication, alert routing,
retention policy, access reviews, and rehearsal frequency.
