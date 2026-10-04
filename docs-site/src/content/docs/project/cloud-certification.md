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
2. Provisions a disposable DigitalOcean Kubernetes or Amazon EKS cluster with
   Terraform, its state kept remotely per run.
3. Starts an in-cluster PostgreSQL and Redis with freshly generated TLS
   material and secrets, then installs the release with production settings.
4. Asserts `/ready` on the gateway and admin, seeds the governed-core fixtures,
   and runs the governed-core end-to-end subset through port-forwards: a
   governed query with redaction and an audit row on every protocol.
5. Re-installs the same digest to prove the upgrade path, including the
   migration job.
6. Uploads redacted evidence, releases the cluster's volumes and destroys
   everything, whether or not certification passed.

The public beta is gated on a DigitalOcean run; general availability also
needs an AWS (EKS) run. The current state of each gate is in the
[release process](/project/release-process/).

## AWS prerequisites

The EKS job assumes an IAM role through GitHub's OIDC provider, so it holds no
long-lived AWS keys. Once per AWS account:

1. An S3 bucket for Terraform state: private, versioned and encrypted.
2. The IAM OIDC provider for `token.actions.githubusercontent.com`, with
   audience `sts.amazonaws.com`.
3. A role the job can assume, trusted only for this repository's
   `cloud-certification` environment, with a maximum session duration of at
   least three hours (the job asks for three, so create, certify and destroy
   run on one session). Check the subject format GitHub issues for the
   repository with `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`:
   with `use_immutable_subject`, the subject is
   `repo:<owner>@<owner_id>/<repo>@<repo_id>:environment:cloud-certification`,
   not `repo:<owner>/<repo>:...`, and a trust policy naming only the classic
   form refuses the job.
4. In the `cloud-certification` environment: the secret `AWS_DEPLOY_ROLE_ARN`
   and the variables `TF_STATE_AWS_BUCKET` and `TF_STATE_AWS_REGION`.

Each EKS run uses one Elastic IP for its NAT gateway, so the region needs one
free under the account's quota.
