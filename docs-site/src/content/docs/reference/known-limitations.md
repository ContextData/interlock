---
title: "Known limitations"
description: "Current limitations of the release candidate, stated plainly."
sidebar:
  order: 2
---

This document records current release-candidate limitations. Normative stable
behavior is defined in [`README.md`](/reference/contracts/).

## Live Certification: What Has Run, And What Has Not

- **Certified live:** PostgreSQL, MySQL, S3/DigitalOcean Spaces, Slack, and
  Google Workspace. PostgreSQL and MySQL cover upstream access, role allow and
  deny, redaction, write safety, approval gating, and audit; S3, Slack and
  Google Workspace cover read, discovery, role denial and audit, with redaction
  additionally proven for Slack and Google Workspace.
- **Not certified live:** Snowflake, Zendesk, GitHub/GitLab,
  OpenSearch/Elasticsearch, Qdrant, Salesforce, and Notion. These still require
  temporary least-privilege credentials and run after local compose
  certification passes.
- **Proven nowhere, not merely un-certified live:** Elasticsearch, GitHub,
  GitLab, Snowflake and Zendesk have no source registered in any test stack, so
  their governance is untested rather than tested-locally-and-awaiting-live.
  See [`connector-support-matrix.md`](/reference/connector-support-matrix/).
- Two cells could not be closed and are recorded as unproven rather than
  quietly dropped: policy deny is not independently attributable on any live
  system, because the blocked identity holds both a blocked source role and a
  matching deny policy; and Google Workspace is Drive-only, because
  domain-wide delegation was not granted for the remaining scopes.
- Live reports must identify sources by source name or secret reference only,
  never by raw credential value.

## Redaction By Protocol

- A policy's `redact_columns` is applied to MCP query results and to HTTP
  proxy JSON responses. The PostgreSQL wire does not apply it: rows returned
  there are redacted only by the PII scanner, which detects values such as
  emails and SSNs by content. A column that holds sensitive values the scanner
  does not recognise is returned unredacted over the PostgreSQL wire. Deny the
  column with a source role or a policy `columns` condition when it must never
  leave, rather than relying on redaction.

## V1 Functional Boundaries

- The canonical feature status list lives in
  [`feature-status.md`](/reference/feature-status/). Capabilities marked `Disabled` or
  `Planned` there must not be represented as production-ready in Admin,
  launch copy, or evaluator docs.
- Approval notifications to Slack are available and beta: a queued, approved,
  rejected, expired or failed approval posts to a webhook or a bot-token
  channel. Delivery is best-effort - it never blocks the agent request, and a
  notification is lost if the process exits before the background post
  completes. Email and generic webhook channels remain deferred.
- Alert rules still have no scheduler and no dispatcher: a rule fires only when
  an administrator clicks Evaluate, and firing records `alert_history` without
  sending anything.
- Some SaaS/search connectors are read/discovery beta surfaces. Unsupported writes must fail closed and should not be advertised as enabled Admin actions.
- Incremental sync is advertised only for connectors with tested stateful delta
  behavior.
- Releases are published to GHCR signed, and DigitalOcean (DOKS) deployment is
  certified for the public beta. AWS (EKS) deployment certification remains a
  gate before general availability; see the
  [release process](/project/release-process/).

## Security Expectations

- Admin and audit surfaces should not render raw secret-like values.
- Audit and approval payload capture must redact secret-like fields and
  size-limit stored bodies.
- Source roles authorize source-scoped access; policy rules never grant source
  access on their own.
- Live artifacts should use disposable namespaces and be cleaned up or
  documented after certification.
