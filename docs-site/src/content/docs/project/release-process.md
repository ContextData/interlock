---
title: "Release process"
description: "The gates a release must pass before it is published, and how to cut one."
normative: true
sidebar:
  order: 5
---

This document is the public record of what must be true before InterLock
publishes a release. It is deliberately conservative: a gate is `Done` only
when it has repeatable evidence, and an open gate blocks publication even if
everything around it is finished.

Current target: **public beta, not GA.** Candidates are published as
`1.0.0-rc.N`; `CHANGELOG.md` records what each one contains.

## Release gates

| Status | Gate | Evidence required |
|---|---|---|
| Done | Public name selected | InterLock is the public product name. UI, onboarding docs, Docker, Helm defaults, and release targets use it. |
| Done | Declared license posture | Apache-2.0 declared in `LICENSE` and `pyproject.toml`. The LGPL-3.0 repository connectors ship in the published image so those connectors work as shipped; `NOTICE` records the obligations and how they are met, and CI still denies GPL-2.0, GPL-3.0 and AGPL-3.0. |
| Done | Security P0s | Protocol admission limits, PostgreSQL extended-protocol Flush governance, SQL safety, connector egress, Admin RBAC/audit, secret masking, audit durability, cache barriers, and worker lease recovery all have behavioral regression coverage. |
| Done | Reproducible local validation | `make final-boss-local` passes from a clean clone, including Docker build, Compose E2E, browser, and load gates. |
| Done | Compose demo path | Clean-volume runs apply all migrations, seed idempotently twice, and pass the E2E suite. [the seeded-stack guide](/guides/evaluate-with-the-seeded-stack/) uses the same tracked lifecycle commands. |
| Done | Helm render | The chart renders with production-safe values and is covered by chart validation tests. |
| Done | Compatibility and operations contracts | The [contracts](/reference/contracts/) publish the stable MCP, HTTP, PostgreSQL, MySQL, configuration, Helm, migration, error, and deprecation contracts. The [runbooks](/operations/runbooks/) publish backup/restore, RPO/RTO, incident, upgrade/rollback, audit-retention, and service-operations procedures. |
| Done | Community files | Contributing guide, security policy, code of conduct, changelog, issue templates, PR template, and threat model are present. |
| Done | Secret scan | `make secret-scan` runs in CI alongside Bandit and pip-audit. |
| **Open** | Automated DigitalOcean (DOKS) deployment and live certification | The automation is complete and validated offline: Terraform for EKS and DOKS with remote per-run state, an ephemeral in-cluster data tier with generated TLS, certification steps that assert readiness, a governed query with redaction and an audit row, and an upgrade, plus redacted evidence upload. What remains is executing it: the gate closes only when a run on DigitalOcean has used protected credentials to provision disposable infrastructure, certify it, upload evidence, and tear down. |
| Deferred | AWS (EKS) deployment certification | Deferred - not required for the public beta. The EKS job in the same workflow is unchanged and has not been run; it becomes a gate before GA. |
| Done | Published artifact provenance | `v1.0.0-rc.1` publishes a multi-arch image whose digest is cosign-signed against the release workflow's OIDC identity and recorded in the Sigstore transparency log, with SLSA v1 provenance and an SPDX SBOM attached in the registry. The provenance names the released commit. GitHub's own attestation store is not used while the repository is private - see the note below - and the release manifest records which mechanisms ran. |

## Launch rules

- Do not publish a public release, PyPI package, container image, Helm chart, or
  public announcement while any gate above is open. A `Deferred` gate is not
  open: it is scheduled after the public beta and does not block it.
- Apache-2.0 is the public-beta license. Do not change license posture without
  owner approval.
- Live certifications run last, use temporary least-privilege credentials, and
  never commit raw credential values. Reports identify sources by name or secret
  reference only.
- Certification evidence is generated into `build/` and is not committed.

## Cutting a release

1. **Bump the version.** Set `version` in `pyproject.toml` to the PEP 440 form
   (`1.0.0rc13`) and `version` and `appVersion` in
   `deploy/helm/interlock/Chart.yaml` to the SemVer form (`1.0.0-rc.13`); a
   test fails until they agree. Run `uv lock` and `make requirements-production`.
2. **Refresh the docs.** `make docs-generate`, and `make docs-screenshots-stack`
   if the console changed. Move the CHANGELOG's `Unreleased` entries under a new
   `## 1.0.0-rc.N - date` heading.
3. **Merge** the release PR once CI, including the end-to-end suite, is green on
   `main`.
4. **Tag** `v1.0.0-rc.N` on that commit and push the tag. `release.yml` runs the
   full local gate again, then builds, signs and publishes the image and chart
   and a release manifest with their digests. Its preflight refuses a tag that
   does not match `pyproject.toml`.
5. **Verify** the published artifacts with `deploy/scripts/release/verify-oci-release.sh`
   and scan the image with Trivy (no fixable critical or high findings).
6. **Certify** it with the cloud certification below, then upgrade any standing
   deployment by digest.

## Running the cloud certification

The run needs, in order:

1. A pushed `v*.*.*` tag so `release.yml` publishes the signed image and chart
   and emits their digests.
2. A `cloud-certification` GitHub Environment holding `AWS_DEPLOY_ROLE_ARN`
   (an IAM role trusting this repository's OIDC) or `DIGITALOCEAN_TOKEN`, plus
   `SPACES_ACCESS_KEY_ID`/`SPACES_SECRET_ACCESS_KEY` for DigitalOcean state.
3. Repository variables `TF_STATE_BUCKET`, `TF_STATE_REGION`, and for
   DigitalOcean `TF_STATE_ENDPOINT`, pointing at a pre-created state bucket.
4. `INTERLOCK_HELM_VALUES_B64` is optional: the run uses
   `deploy/helm/values.certification.yaml` and treats the secret as an
   overlay for provider-specific values such as a storage class.

Then dispatch the workflow with the release version and the two digests. State
is keyed per run, so a cancelled run can still be destroyed from a later one.

Before spending anything, run the offline gates: they catch a broken workflow
or Terraform root without provisioning:

```bash
make check-infra
```

## Artifact provenance

A release publishes provenance by three mechanisms, and a consumer can check
which ones a given release used by reading `provenance` in its
`release-manifest.json` rather than guessing:

| Mechanism | Where it lives | How to verify |
| --- | --- | --- |
| Cosign signature | Sigstore, Rekor transparency log | `deploy/scripts/release/verify-oci-release.sh` |
| SLSA v1 provenance | Attached to the image in the registry | `docker buildx imagetools inspect <ref> --format '{{ json .Provenance }}'` |
| SPDX SBOM | Attached to the image in the registry | same command with `.SBOM` |
| GitHub attestations | GitHub's attestation store | `gh attestation verify` |

The first three run on every release. **The fourth is skipped while this
repository is private**, because GitHub's attestation store is a paid feature
for private repositories; the workflow steps are conditional on the repository
being public and resume with no edit once it is. This is a duplicate storage
and verification path for provenance that already exists in the registry, not
an absence of provenance: the SLSA predicate published with `v1.0.0-rc.1`
carries `vcs:revision`, `github_workflow_sha` and a builder id naming the exact
workflow run, which is the binding to the released commit the gate requires.

## Supply chain

- Production dependencies are installed from `uv.lock`. `requirements-production.txt`
  is a hash-pinned export, and `make requirements-production-check` fails the
  build if it drifts.
- `make release-evidence` produces a CycloneDX SBOM, a `pip-audit` CVE report,
  and a license report under `build/release-evidence/`.
- No active production dependency CVE waivers are accepted. Any future waiver
  must record the package, vulnerability identifiers, direct or transitive
  status, reason, owner, and expiry before a release tag.
- `pip-audit` currently reports no known vulnerabilities. The three most recent
  remediations - PYSEC-2026-3721, CVE-2026-9856 and CVE-2026-15925 - were
  resolved by raising floors inside the existing ceilings rather than by
  widening a constraint, and the pinned floors are asserted by
  `tests/unit/test_mvp_status_docs.py` so a later downgrade fails the build.
- Heavy extractor and ML dependencies belong to the worker profiles
  (`production-worker`, `production-worker-ml`), not the Gateway/Admin runtime
  image.
- The only copyleft dependencies are PyGithub and python-gitlab (LGPL-3.0),
  which ship in `production`, the pinned export, and the published image so the
  GitHub and GitLab connectors work as shipped. `NOTICE` records how the
  LGPL-3.0 obligations are met; InterLock itself remains Apache-2.0. A
  copyleft-free build is still possible by installing `production` without
  `connectors-repo`, at the cost of those two connectors. Strongly reciprocal
  licenses (GPL-2.0, GPL-3.0, AGPL-3.0) remain denied in CI.

## Feature honesty

A capability is not advertised as enabled unless [`feature-status.md`](/reference/feature-status/) and
`src/interlock/feature_status.py` both list it as `Beta` or `Certified` with matching
evidence. "Matching" is now enforced rather than asserted:
`tests/unit/test_feature_status_reconciliation.py` compares the two field by
field and fails on any difference. Before it existed the two had drifted across
six capabilities - including one where the published limitation was more
flattering than the registry's, and one that told operators to install a
package name that stopped existing at the rename. Anything `Disabled` or `Planned` stays hidden, appears disabled in the
Admin UI, or is explicitly labeled. Known boundaries are recorded in
[`known-limitations.md`](/reference/known-limitations/).

If a feature moves between states, update the code registry, this document set,
the connector support matrix where applicable, and the relevant tests in the
same change.
