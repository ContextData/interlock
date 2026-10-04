---
title: Deploy with Helm
description: Install InterLock on Kubernetes from the signed Helm chart and image.
sidebar:
  order: 7
---

The chart installs the gateway (a StatefulSet, for its audit-spool volume), the
admin, the workers, and a migration job that runs before each install and
upgrade. It always runs in production mode. PostgreSQL and Redis are yours to
provide.

For a complete walkthrough on DigitalOcean, from an empty account to a
running deployment with TLS, see
[Deploy on DigitalOcean Kubernetes](/operations/deploy-on-digitalocean-kubernetes/).

## Before installing

Create, in the release's namespace:

| Secret | Keys | Holds |
|---|---|---|
| `interlock-db-credentials` | `username`, `password` | the control-database login |
| `interlock-admin-secret` | `secret-key` | 32+ characters; signs admin sessions |
| `interlock-api-key-pepper` | `pepper` | 32+ characters; never change it casually |
| `interlock-control-db-ca` | `ca.crt` | the CA of the control database's certificate |
| `interlock-pg-listener-tls` | `tls.crt`, `tls.key` | the certificate the gateway presents on its PostgreSQL port |

and a StorageClass with `ReadWriteOnce` for the audit spool.

Start from `deploy/helm/values.cloud.example.yaml`, which documents every value
you must set and references the Secrets by name, so the values file holds no
credentials. Source credentials reach the pods through `extraEnvFrom` and are
referenced from sources as `env://NAME`.

## Install a signed release

Releases publish the chart and image to GHCR, signed with cosign. Deploy by
digest, and verify first:

```bash
deploy/scripts/release/verify-oci-release.sh ContextData/interlock \
  ghcr.io/contextdata/interlock-runtime@sha256:<image> \
  ghcr.io/contextdata/charts/interlock@sha256:<chart>

deploy/scripts/release/deploy-oci-release.sh \
  --chart-ref oci://ghcr.io/contextdata/charts/interlock --chart-digest sha256:<chart> \
  --image-repository ghcr.io/contextdata/interlock-runtime --image-digest sha256:<image> \
  --values my-values.yaml --namespace interlock --release interlock
```

Without a digest or tag, the chart uses the image released with it (its
`appVersion`).

## After installing

- Sign in and change the default admin password, or set
  `admin.bootstrapPasswordExistingSecret` before the first install and remove it
  afterwards.
- Expose the console only where it should be reachable (`admin.ingress` with a
  source-range restriction, or a private load balancer).
- A change to chart configuration rolls the pods; a change to a Secret does
  not: `kubectl rollout restart` after rotating one.

Every value is specified in the [Helm contract](/reference/contracts/helm-v1/).
