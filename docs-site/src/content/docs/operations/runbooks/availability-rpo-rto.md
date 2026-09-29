---
title: "Availability, RPO, And RTO"
description: "Availability targets, recovery point and recovery time objectives."
normative: true
sidebar:
  order: 2
---

Status: V1 reference objectives for a production deployment.

These are deployment objectives, not guarantees of the open-source software.
An operator must replace them with stricter organizational requirements where
applicable and prove the selected architecture meets them.

## Reference Objectives

| Component/data | Reference RPO | Reference RTO | Required design |
| --- | ---: | ---: | --- |
| Control PostgreSQL metadata, roles, policies, approvals | 15 minutes | 4 hours | HA managed PostgreSQL, point-in-time recovery, daily verified backup |
| Audit database and durable spool | 5 minutes | 4 hours | Strict audit mode, persistent per-Gateway spool, database PITR, spool monitoring |
| Gateway/Admin availability | No durable state | 60 minutes | At least two Gateway replicas, replaceable images, externalized state |
| Worker queue and ingestion state | 15 minutes | 4 hours | Control DB backup plus lease recovery and idempotent reprocessing |
| Redis sessions, L2 cache, generations, invalidation state | 15 minutes | 2 hours | HA Redis with persistence; strict writes fail when generation barrier is unavailable |
| Discovery vectors/index snapshots | 24 hours | 8 hours | Persisted vector source plus rebuild procedure from discovery records |
| Source credentials and encryption material | Per secret-store policy | 4 hours | Versioned external secret store and documented rotation |

An operator claiming a lower RPO must demonstrate continuous replication and
recovery, including the audit spool. A snapshot schedule alone cannot support
an RPO shorter than its interval.

## Failure Semantics

- Loss of Redis may evict cache and Admin sessions. It must not grant access.
- With the strict cache barrier enabled, writes are rejected while source
  generation cannot be advanced safely.
- With strict audit durability, governed work is rejected when neither the
  audit database path nor persistent spool can accept an event.
- Loss of a Worker does not make Gateway traffic unavailable. Expired leases
  are reclaimed within configured retry bounds.
- Loss of vector search should degrade discovery readiness or result quality;
  it must not bypass source-role or policy filtering.

## Minimum Production Topology

- Two or more Gateway replicas in distinct failure domains.
- At least two Workers when ingestion continuity matters.
- Managed or equivalently operated HA PostgreSQL with PITR.
- Managed or clustered Redis with authentication, TLS, and persistence.
- Persistent audit spool per Gateway replica.
- External secret management.
- TLS at every client and dependency boundary.
- Monitoring for readiness, audit health, cache barrier, worker lease age,
  storage usage, migration head, and source circuit state.

## Quarterly Recovery Exercise

1. Select the latest released digest and a representative backup.
2. Restore PostgreSQL and Redis into an isolated network.
3. Restore/replay pending audit spool records idempotently.
4. Rebuild or reload discovery vectors.
5. Start the exact release image and verify migration head.
6. Run allowed, denied, redacted, cached, queued, approved, and audited paths.
7. Measure actual recovery point and elapsed recovery time.
8. Record gaps and update this table or the deployment design.
