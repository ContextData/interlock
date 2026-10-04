---
title: "InterLock 1.0.0-rc.1 Release Path"
description: "What a V1 release candidate must satisfy before it is published."
normative: true
sidebar:
  order: 11
---

Status: normative release process for the first V1 candidate.

## Candidate Freeze

`1.0.0-rc.1` freezes the public contracts in this directory. Beta connector
implementation may continue, but changes to stable protocol behavior require a
contract review and release note.

The candidate commit must be clean, reproducible, and free of live credentials,
private hostnames, private repository names, local credential paths, and
uncurated certification artifacts.

## Build Identity

One source commit produces:

- Python wheel and source distribution.
- Gateway/Admin/Worker runtime image.
- OCI Helm chart.
- SBOM and dependency-license report.
- Vulnerability and secret-scan reports.
- Provenance/attestation and signatures.
- Local, browser, load, cloud, and live certification reports.

Every artifact and report records the Git SHA and immutable image/chart digest.
Gateway, Admin, Worker, migration, Compose, and cloud certification use the
same image digest.

## Promotion Gates

1. Locked dependency installation.
2. Formatting, lint, expanded typecheck, unit, integration, security, audit,
   migration, and doc-contract guards.
3. No unaccepted high or critical production dependency vulnerability.
4. Docker build and Helm render from the candidate commit.
5. Clean Compose boot, idempotent seed, and E2E twice.
6. Desktop and 390px Admin browser certification.
7. Concurrency, outage, memory, latency, and soak evidence.
8. Disposable DigitalOcean (DOKS) deployment from the signed artifacts for the
   public beta. Disposable AWS (EKS) deployment is required before GA; it was
   first certified with `1.0.0-rc.16`.
9. Stable-core live certification last, using temporary least-privilege
   credentials and no secret-bearing reports.

Failure of a gate creates a new release candidate. Do not mutate or silently
replace a published candidate artifact.

## RC Compatibility

Release candidates may contain breaking corrections before `1.0.0`, but every
change from a prior candidate must be documented. Once `1.0.0` is published,
the V1 compatibility and one-minor deprecation policy applies.

## Promotion To 1.0.0

Promotion requires:

- No open P0 finding.
- No undocumented P1 security or data-integrity limitation.
- Stable interfaces certified from the exact candidate commit.
- Beta/deferred labels matching runtime capability.
- Operations runbooks reviewed by an operator other than the author.
- Backup restore and upgrade rollback rehearsed on a disposable environment.
- Public security contact and support expectations published.

The release report belongs under `build/certification/` and may
reference secret names, never secret values.
