---
title: "Audit Retention And Immutability Runbook"
description: "Keeping, archiving and protecting the audit log."
normative: true
sidebar:
  order: 1
---

## Goals

- Preserve a queryable record of every governed allow, deny, cache, approval,
  execution, redaction, and error outcome.
- Prevent application identities from rewriting history.
- Keep retention predictable without allowing partition rollover to drop
  events.
- Dispose of expired records under an approved policy.

## Reference Retention Policy

The open-source runtime does not impose one legal retention period. The V1
reference policy is:

- 90 days online/queryable.
- 365 days encrypted archive.
- Longer retention only where legal, contractual, or incident hold requires.
- Approval/control-plane audit retained at least as long as corresponding
  governed request audit.

Operators must document jurisdiction, data classification, litigation hold,
and deletion requirements. Audit payload capture should remain minimized and
redacted even when retention is long.

## Database Permissions

Production application roles should have INSERT and SELECT needed for service
operation, but no UPDATE or DELETE on audit rows. Partition creation/retention
uses a separate maintenance role. Database administrators and backup systems
remain privileged and must be independently audited.

## Partition Maintenance

1. Verify previous, current, and configured future monthly partitions exist.
2. Verify the default partition is present as a safety net.
3. Monitor records entering the default partition; any sustained use is a
   maintenance incident.
4. Run maintenance under its PostgreSQL advisory lock.
5. Check partition indexes and storage growth after maintenance.

Do not drop a partition merely because its month is old. Confirm archive,
legal hold, and approved deletion first.

## Durable Spool Operations

- Alert on spool write failure, backlog count/age, dead-letter count, and disk
  usage.
- In strict mode, inability to use both database and spool rejects governed
  work.
- Replay uses stable event IDs and is idempotent.
- Quarantine poison records to dead letter; never delete silently.
- Encrypt snapshots and restrict volume access to the Gateway service identity
  and recovery operators.

## Archival

1. Freeze the closed partition against further application writes.
2. Export using a format that preserves timestamps, JSON metadata, event IDs,
   and checksums.
3. Encrypt with a managed key and write to immutable/versioned storage.
4. Record row count, byte count, time range, schema version, and cryptographic
   checksum.
5. Restore a sample into an isolated environment and query it.
6. Mark the archive verified before online deletion is eligible.

## Disposal

Disposal requires policy approval, no active hold, verified archive status,
and a maintenance window. Record the partition/time range, approver, checksum,
deletion time, and storage deletion evidence in a separate administrative
audit system. Never use application endpoints for bulk audit deletion.

## Monthly Verification

- Partition coverage and default-partition row count.
- Buffer retries, spool backlog, dead letters, and oldest pending age.
- Application audit table privileges.
- Archive restore sample.
- Retention exceptions and legal holds.
- Correlation continuity across logs, traces, approvals, and audit events.
