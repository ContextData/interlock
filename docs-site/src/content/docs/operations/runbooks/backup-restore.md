---
title: "Backup And Restore Runbook"
description: "Backing up and restoring the control database."
normative: true
sidebar:
  order: 3
---

## Backup Scope

Back up all of the following as one documented recovery set:

- Control PostgreSQL database, including `schema_migrations`, identities,
  grants, source roles, policies, approvals, ingestion jobs, discovery
  metadata, cache dependencies, and audit partitions.
- PostgreSQL WAL or provider PITR logs.
- Redis persistence when Admin session continuity and cache-generation state
  are in scope.
- Every Gateway audit spool persistent volume.
- External secret-store versions and encryption-key references.
- Helm values with secret values removed, chart version, image digest, and
  release manifest.
- Persisted vector/index data or the records and model metadata required to
  rebuild it.

Never place unencrypted database dumps, private keys, or source credentials in
the repository or certification reports.

## Schedule

Reference minimum:

- PostgreSQL PITR continuously, full backup daily, retained per policy.
- Redis persistence continuously and provider snapshot at least daily.
- Audit spool monitored continuously and included in volume snapshots at least
  hourly until delivered.
- Release manifests and SBOM retained for every deployed version.
- Restore rehearsal quarterly and before V1 promotion.

The actual interval must satisfy [the selected RPO](/operations/runbooks/availability-rpo-rto/).

## Pre-Backup Checks

1. Confirm migration head and database health.
2. Confirm audit partitions cover current and future months.
3. Confirm audit queue/spool backlog and filesystem usage.
4. Record release SHA, image digest, chart version, and backup timestamp.
5. Confirm encryption and destination retention policy.

## Isolated Restore Procedure

1. Create a network-isolated recovery environment with no routes to live data
   sources.
2. Restore the control PostgreSQL backup to a new database instance.
3. Restore Redis only when its snapshot is known compatible; otherwise start
   empty and expect session/cache loss.
4. Attach copies of audit spool volumes. Never mount a production spool
   read-write into a rehearsal.
5. Deploy the exact image digest that produced the backup.
6. Verify `schema_migrations` checksums before applying newer migrations.
7. Replay audit spool events and verify idempotent event IDs prevent duplicate
   logical audit records.
8. Reload or rebuild discovery vectors using matching embedding dimensions.
9. Start services with external source egress disabled.
10. Verify `/ready`, Admin login, role/policy reads, audit history, approval
    state, worker queue state, and discovery metadata.

## Recovery Validation

The restore is not complete until:

- Row counts and critical-table checksums match the selected recovery point.
- No migration checksum mismatch exists.
- Active grants and policies produce expected allow and deny decisions.
- Audit events through the recovery point are queryable.
- Spool replay leaves no unexplained failed/dead-letter event.
- Pending approvals do not execute during validation.
- Secrets were restored by reference and no plaintext secret appears in logs.
- Measured RPO/RTO is recorded.

## Production Recovery

After incident command approves recovery:

1. Freeze writes and connector sync where possible.
2. Select the recovery point based on integrity, not merely recency.
3. Restore into replacement services rather than overwriting the only copy.
4. Run isolated validation.
5. Rotate credentials if compromise is possible.
6. Switch traffic using a controlled DNS/service change.
7. Watch audit, cache generation, worker leases, errors, and source circuits.
8. Preserve old volumes and logs until incident closure.

## Failure And Escalation

If backup decryption, migration verification, audit replay, or integrity checks
fail, stop. Do not route production traffic. Escalate under
[incident-response.md](/operations/runbooks/incident-response/) and retain all evidence.
