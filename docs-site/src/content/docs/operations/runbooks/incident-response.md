---
title: "Security And Reliability Incident Response"
description: "Responding to security and reliability incidents."
normative: true
sidebar:
  order: 4
---

## Severity

| Severity | Examples | Initial response target |
| --- | --- | ---: |
| SEV-0 | Active credential theft, authorization bypass, unredacted sensitive-data exposure, audit integrity loss | 15 minutes |
| SEV-1 | Governed writes executing incorrectly, widespread outage, unrecoverable audit/cache barrier failure | 30 minutes |
| SEV-2 | Partial source/connector outage, worker backlog, degraded discovery, bounded audit spool growth | 4 hours |
| SEV-3 | Non-security defect with workaround, documentation or certification gap | 1 business day |

Targets are operator objectives and require an on-call process.

## Immediate Actions

1. Assign incident commander, operations lead, security lead, and recorder.
2. Capture time, release SHA, image digest, affected components, correlation
   IDs, and safe symptoms.
3. Contain exposure:
   - disable affected source or identity,
   - revoke/rotate compromised credentials,
   - block connector egress,
   - pause approvals and ingestion,
   - reject governed traffic if audit integrity is uncertain.
4. Preserve logs, traces, audit partitions, dead letters, spool volumes,
   manifests, and database snapshots read-only.
5. Do not paste credentials, raw PII, or approval payloads into tickets/chat.

## Investigation Checklist

- Did source roles, policy, rate limit, write safety, redaction, cache, and audit
  produce the same decision for the affected request?
- Was the identity or grant recently changed or cached?
- Did a cache key cross source, identity, role, grant, or policy scope?
- Did egress validation or DNS resolution change between validation/connect?
- Did audit enter the database, spool, retry queue, or dead-letter path?
- Did a Worker lose its lease or complete after cancellation?
- Is the deployed digest the certified digest?
- Were migrations and configuration identical across replicas?

## Containment By Failure Type

- **Authorization/redaction:** disable source/identity, stop Gateway traffic,
  invalidate source cache generation, preserve audit, patch before resume.
- **Audit unavailable:** strict mode rejects new governed work; preserve spools,
  restore PostgreSQL, replay idempotently, verify partition health.
- **Redis/cache:** reject writes when the consistency barrier is unavailable;
  restore Redis, advance source generations, invalidate affected source caches.
- **Worker/ingestion:** pause claims, expire/reclaim leases deliberately,
  quarantine poison jobs, verify source asset versions before replay.
- **Credential exposure:** revoke at provider first, rotate InterLock references,
  search audit/log/UI artifacts, notify affected owners.

## Recovery

Recovery requires explicit incident-command approval and:

- Root cause or safe containment is understood.
- Compromised credentials are rotated.
- Fixed artifact is reviewed and identified by digest.
- Audit continuity is reconciled.
- Cache generations are safe.
- Smoke and adversarial regression tests pass.
- Monitoring is heightened for a defined observation period.

## Communication And Disclosure

Use the process in the repository `SECURITY.md` for vulnerability disclosure.
Public communication must state affected versions, impact, mitigations, fixed
version, and upgrade instructions without exposing customer or source details.

## Post-Incident

Within five business days for SEV-0/1, publish an internal review covering
timeline, detection gap, control behavior, contributing factors, corrective
owners/dates, regression tests, and runbook changes. Security advisories should
be public when users need action.
