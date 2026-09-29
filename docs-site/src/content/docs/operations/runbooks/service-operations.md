---
title: "Service Operations Runbook"
description: "Day-to-day operation of each service and its dependencies."
normative: true
sidebar:
  order: 5
---

## Health Endpoints

- `/health` proves the process is alive. It must remain shallow.
- `/ready` proves mandatory dependencies and schema state are ready for
  traffic. It should cover PostgreSQL, Redis, migration head, source/policy
  listeners, audit health, invalidation listener, and vector freshness where
  required by the service role.
- `/stats` is operationally sensitive and must be disabled publicly or exposed
  only on an authenticated/internal listener.

Do not route traffic based only on `/health`.

## Daily Checks

- All service replicas use the same image digest and configuration generation.
- Readiness is healthy and migrations match the image head.
- Audit queue/spool/dead-letter counts are within thresholds.
- Redis cache generation and invalidation subscribers are connected.
- Worker heartbeat, active jobs, lease expiry, retry, and poison counts are
  plausible and consistent with Admin.
- Discovery vector generation matches persisted metadata.
- PostgreSQL/Redis storage, connection pools, latency, and error rates have
  capacity headroom.
- Source circuit breakers and egress denials are reviewed.
- Certificate and secret expirations are inside alert windows.

## Capacity Thresholds

Set deployment-specific alerts before saturation. Reference starting points:

- Audit spool disk warning at 60%, critical at 80%.
- Database pool sustained usage warning at 70%, critical at 90%.
- Redis memory warning at 70% of eviction-safe capacity.
- Worker oldest queued job warning at twice the normal processing SLO.
- Gateway p95 latency and rejection rate compared with certified load profile.
- Repeated cache barrier or partition-maintenance failure is critical.

## Dependency Failure Matrix

| Failure | Required behavior |
| --- | --- |
| Control PostgreSQL | Readiness fails; strict audit uses spool only within capacity; control mutations stop |
| Redis | Readiness degrades/fails by role; cache misses are safe; strict governed writes reject without generation barrier |
| Audit spool | Readiness fails in strict mode if DB delivery is also unavailable |
| Upstream source | Circuit opens; source requests return sanitized failure; other sources remain isolated |
| Worker | Lease expires and eligible work is reclaimed; Gateway traffic continues |
| Vector index | Discovery degrades or readiness fails; authorization never bypasses filters |

## Restart Order

1. Control PostgreSQL and Redis.
2. Migration Job and schema-head verification.
3. Gateway, watching audit replay and invalidation subscriptions.
4. Admin.
5. Workers, initially at bounded concurrency.
6. Resume source sync and queued approvals only after health review.

## Safe Maintenance

- Disable or drain a source before credential or endpoint changes.
- Advance/invalidate source cache generation after changes affecting result
  authorization or origin data.
- Pause ingestion before vector dimension/model changes.
- Never bypass strict audit or cache barriers to finish a maintenance window.
- Record changes with safe before/after summaries and correlation IDs.
