---
title: "Helm Chart V1 Contract"
description: "The Helm chart values and behaviour promised in V1."
normative: true
sidebar:
  order: 5
---

Status: stable release-candidate deployment surface.

Chart: `deploy/helm/interlock`
Public chart name: `interlock`

The chart deploys one shared runtime image with service-specific commands:

- Gateway StatefulSet.
- Admin Deployment.
- Worker Deployment and optional HPA.
- Pre-install/pre-upgrade migration Job.
- Gateway and Admin Services.
- Optional Ingress.

The internal template helper name `onyx` is not public branding and may remain
through V1.

## Required Production Inputs

| Value | Requirement |
| --- | --- |
| `image.repository` | Registry repository containing the InterLock runtime |
| `image.digest` | SHOULD pin the certified immutable digest |
| `database.existingSecret` | Secret containing configured username/password keys |
| `database.caExistingSecret` | Secret containing the control DB CA |
| `database.sslMode` | MUST be `verify-full` in production |
| `admin.secret.existingSecret` | Secret containing the Admin signing key |
| `auth.apiKeyPepperExistingSecret` | Secret containing the API-key HMAC pepper |
| `gateway.pgTls.existingSecret` | PostgreSQL listener TLS secret unless trusted offload is intentional |
| `redis.url` | Reachable Redis endpoint; ignored when `redis.existingSecret` is set |
| `redis.existingSecret` | Secret holding the full Redis URL under `redis.secretUrlKey`; when set, the URL is omitted from the ConfigMap |
| `imagePullSecrets` | Pull secrets attached to every pod, the pre-install migration Job included |
| `gateway.extraEnv` / `gateway.extraEnvFrom` | Additional environment for the Gateway; how `env://` connector references are supplied |
| `admin.extraEnv` / `admin.extraEnvFrom` | The same for Admin, which resolves those references when probing a source |
| `worker.extraEnv` / `worker.extraEnvFrom` | The same for the Worker |
| `admin.bootstrapPasswordExistingSecret` | Secret holding the first admin user's password; without it the first admin is `admin` / `admin` and MUST change the password at first sign-in. SHOULD be removed once an administrator has signed in |
| `gateway.service.annotations` | Annotations on the combined HTTP/PostgreSQL Service |
| `gateway.pgService.enabled` | Publishes a second Service carrying only the PostgreSQL wire port |
| `admin.ingress.enabled` | Publishes a separate Ingress for the Admin console |

The chart fails rendering when required secret references or values are absent
where the templates can validate them. Kubernetes admission and readiness are
the final validation boundary for externally managed secrets.

## Stable Values

The following roots are stable throughout `1.x` and follow the deprecation
policy:

- `image`
- `migration`
- `gateway`
- `admin`
- `worker`
- `database`
- `auth`
- `redis`
- `config`
- `ingress`
- `podSecurityContext`
- `containerSecurityContext`

Additive values are compatible. A value changing type or meaning is breaking.

## Audit Spool

`gateway.auditSpool.enabled=true` mounts writable storage at
`gateway.auditSpool.mountPath`. Without `existingClaim`, the Gateway
StatefulSet creates one RWO claim per replica. An existing shared claim should
be used only with storage semantics suitable for concurrent pods.

Strict audit durability requires readiness to fail when neither PostgreSQL nor
the pod's durable spool can accept audit events. Operators must include spool
volumes in recovery and capacity procedures; they are not disposable scratch
space.

## Migrations

When `migration.enabled=true`, a Helm hook Job runs before install and upgrade.
It uses the release image and `python -m interlock.db.migrate`. Application
workloads must not become ready against a schema behind their bundled migration
head.

Do not run two independently versioned chart upgrades against the same control
database. The migration runner uses a PostgreSQL advisory lock, but operators
remain responsible for release coordination and backup.

## Probes And Security Context

- `/health` is liveness only.
- `/ready` is dependency-aware readiness.
- Containers run non-root, drop Linux capabilities, prohibit privilege
  escalation, use the runtime-default seccomp profile, and use a read-only root
  filesystem where the workload supports it.
- Writable paths are explicit volumes.

## Image Compatibility

Gateway, Admin, Worker, and migration Job for one release MUST use the same
image digest. Chart `appVersion`, release notes, SBOM, provenance, and the
certification report must identify that digest. `latest` is not a production
release value.

## Upgrade And Rollback

Follow [the upgrade runbook](/operations/runbooks/upgrade-rollback/). Helm rollback
does not reverse database migrations. A rollback is permitted only when the
older application version is compatible with the migrated schema or after a
tested database restore.

The Gateway StatefulSet's `volumeClaimTemplates` carry only stable labels
(`app.kubernetes.io/name`, `app.kubernetes.io/instance`,
`app.kubernetes.io/component`). That field is immutable, so anything that varies
per release there would make every release-to-release upgrade fail. Deployments
created by a chart before `1.0.0-rc.5` need a one-time replacement of the
StatefulSet object when first upgraded; the release deploy script performs it,
and the upgrade runbook describes it.

## Configuration Changes

The Gateway, Admin, and Worker pod templates carry a `checksum/config`
annotation computed from the rendered ConfigMap, so a `helm upgrade` that changes
only configuration values rolls those workloads. Configuration reaches the
containers through `envFrom`, which a container reads once at start; without the
checksum such an upgrade reported success while the running pods kept the old
values.

Secrets are not part of that checksum. Production Secrets are referenced by name
and never rendered by the chart, so rotating one - or anything supplied through
`extraEnvFrom` - requires `kubectl rollout restart` of the affected workloads.

## Additive Operator Values

The values above marked additive were introduced after a real managed-Kubernetes
deployment. Each defaults to off, so an existing release upgrades unchanged;
[`compatibility-policy.md`](/reference/contracts/compatibility-policy/) treats that as compatible.

Two carry operational obligations rather than being merely optional:

- `admin.bootstrapPasswordExistingSecret` sets the first admin account's
  password and is then inert; remove both the value and the Secret once an
  administrator has signed in. Without it the first admin is `admin` with the
  default password `admin`, which must be changed at first sign-in before the
  console is usable - so sign in and change it before exposing the console.
  Admins change their own password at `/auth/change-password`.
- `admin.ingress.*` exposes the Admin console. The console is an
  administrative surface; restrict it with a source range, an authentication
  middleware, or a private load balancer rather than publishing it openly.

`redis.existingSecret` exists because `redis.url` is rendered into a ConfigMap.
A managed Redis carries its password inside the URL, and a ConfigMap is
readable by anyone who can read the namespace. When the Secret is set the
ConfigMap omits the key entirely.
