---
title: Cloud certification
description: How a release is certified on a disposable Kubernetes cluster before publication.
sidebar:
  order: 6
---

Before a release is announced, the signed image and chart are deployed to a
disposable cluster, certified, and torn down, by the `cloud-certification`
workflow. It runs on demand with the release version and the two digests.

What a run does, in order:

1. Verifies the cosign signatures of the image and chart against this
   repository's release workflow.
2. Provisions a disposable DigitalOcean Kubernetes cluster with Terraform, its
   state kept remotely per run.
3. Starts an in-cluster PostgreSQL and Redis with freshly generated TLS
   material and secrets, then installs the release with production settings.
4. Asserts `/ready` on the gateway and admin, seeds the governed-core fixtures,
   and runs the governed-core end-to-end subset through port-forwards: a
   governed query with redaction and an audit row on every protocol.
5. Re-installs the same digest to prove the upgrade path, including the
   migration job.
6. Uploads redacted evidence, releases the cluster's volumes and destroys
   everything, whether or not certification passed.

The public beta is gated on a DigitalOcean run. An AWS (EKS) job exists in the
same workflow and becomes a gate before general availability. The current
state of each gate is in the [release process](/project/release-process/).
