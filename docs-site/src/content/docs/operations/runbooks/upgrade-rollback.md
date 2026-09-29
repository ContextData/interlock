---
title: "Upgrade And Rollback Runbook"
description: "Upgrading InterLock and rolling back safely."
normative: true
sidebar:
  order: 6
---

## Preconditions

- Target image/chart is signed, attested, vulnerability-scanned, and identified
  by immutable digest.
- Release notes identify schema and compatibility changes.
- Current release is within the supported upgrade window.
- A restorable pre-upgrade PostgreSQL backup and spool snapshot exist.
- Migration was tested against a restored production-shaped database.
- Capacity, maintenance window, rollback owner, and success criteria are set.

## Upgrade Procedure

1. Record current release SHA, image digest, chart version, migration head, and
   health status.
2. Pause nonessential ingestion and resolve or record long-running approvals.
3. Confirm audit queue/spool is healthy and below capacity thresholds.
4. Take/verify the pre-upgrade backup.
5. Render the target Helm chart with production values and policy checks.
6. Run the pre-upgrade migration Job. The advisory lock prevents concurrent
   migration runners, but not conflicting operator actions.
7. Verify migration checksums and head.
8. Roll out Gateway, Admin, and Workers using the same image digest.
9. Wait for dependency-aware readiness before admitting traffic.
10. Run smoke paths: authentication, allow, deny, redaction, cache miss/hit,
    queued approval, rejection, audit detail, and discovery.
11. Resume ingestion and monitor for one observation window.

## Configuration-Only Changes

A `helm upgrade` that changes only ConfigMap-backed values rolls the Gateway,
Admin, and Workers through the `checksum/config` pod annotation; confirm the
rollout completes and readiness recovers as in steps 8-9.

Rotating a Secret, or changing anything supplied through `extraEnvFrom`, does
not roll anything, because the chart cannot see those objects. After such a
change run `kubectl rollout restart` for each affected workload and verify the
running environment picked it up.

## Upgrading From A Release Before 1.0.0-rc.5

Charts before `1.0.0-rc.5` put per-release labels (`helm.sh/chart`,
`app.kubernetes.io/version`) on the Gateway StatefulSet's `volumeClaimTemplates`.
Kubernetes forbids changing that field, so upgrading such a deployment to any
newer chart fails and Helm rolls back:

```
StatefulSet.apps "<release>-gateway" is invalid: spec: Forbidden: updates to
statefulset spec for fields other than 'replicas', ... are forbidden
```

`deploy/scripts/release/deploy-oci-release.sh` handles the transition. Before
`helm upgrade` it reads the live Gateway claim template, and only if it still
carries one of those labels it runs:

```bash
kubectl --namespace <namespace> delete statefulset <release>-gateway --cascade=orphan
```

This deletes the StatefulSet object alone. The Gateway pod and its audit spool
volume keep running and serving, and the StatefulSet `helm upgrade` recreates
adopts them because its selector is unchanged. The volume keeps its data. Run
the same command yourself before `helm upgrade` if you do not use the script.
It happens once: charts from `1.0.0-rc.5` onward carry only stable labels there.

## Rollback Decision

Rollback when any of the following persists beyond the defined observation
threshold:

- Mandatory readiness cannot become healthy.
- Authorization, redaction, write safety, or audit is incorrect.
- Error/latency/resource rates exceed the release threshold.
- Worker leases or audit spool backlog grow without recovery.
- Data integrity or migration verification is uncertain.

Security/integrity defects take precedence over availability. Stop governed
traffic if continuing could expose or corrupt data.

## Application-Only Rollback

Use Helm/application rollback only if release notes state the previous version
is compatible with the new schema.

1. Freeze new writes.
2. Roll back all runtime roles to the prior immutable digest.
3. Verify readiness and migration compatibility.
4. Run the smoke matrix.
5. Resume traffic and retain incident evidence.

## Database Restore Rollback

If the prior application cannot read the migrated schema:

1. Stop Gateway, Admin mutations, Workers, and migration jobs.
2. Preserve the failed database and audit spool for investigation.
3. Restore the verified pre-upgrade database to a replacement instance.
4. Reconcile audit spool events created after the backup. Do not blindly replay
   writes or approvals.
5. Deploy the prior application digest.
6. Validate grants, policies, approval terminal states, audit continuity, and
   cache generations before traffic.

V1 has no automatic down-migration contract.

## Post-Change Record

Record timestamps, operators, backup ID by safe reference, old/new digests,
migrations applied, smoke results, rollback decision, and unresolved issues.
Do not record secrets, raw tokens, or private payloads.
