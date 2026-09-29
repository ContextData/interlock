---
title: "Contracts"
description: "The V1 public compatibility contract: what is stable, what is beta, and what may change."
normative: true
sidebar:
  order: 0
---

Status: release-candidate contract for `1.0.0-rc.1`
Contract version: `v1`
Last reviewed: 2026-08-13

This directory defines the public compatibility surface for InterLock V1. It
is normative for the public API and deployment interfaces listed below. It is
not a promise that every modeled or beta connector is production-certified.

The words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are used as described by
RFC 2119 and RFC 8174.

## V1 Product Boundary

### Stable

- Shared source-role, policy, write-safety, redaction, deterministic-cache,
  and audit governance pipeline.
- PostgreSQL wire proxy subset described in
  [postgresql-v1.md](/reference/contracts/postgresql-v1/).
- HTTP source proxy described in [http-proxy-v1.md](/reference/contracts/http-proxy-v1/).
- MCP tool and transport surface described in [mcp-v1.md](/reference/contracts/mcp-v1/).
- MySQL/MariaDB connector semantics described in
  [mysql-connector-v1.md](/reference/contracts/mysql-connector-v1/).
- Admin, configuration, migration, and Helm contracts described here.
- S3 and DigitalOcean Spaces read and discovery paths. Governed object writes
  are not reachable in V1: an approved write fails because neither the MCP
  path nor the approval queue supplies the operation the connector needs, so
  no object mutation is part of the stable contract.

### Beta

The beta connectors are API-compatible within V1, but their source-specific
coverage and live certification depth may change. See
[the connector matrix](/reference/connector-support-matrix/).

- Slack
- GitHub and GitLab
- Snowflake
- Zendesk
- OpenSearch and Elasticsearch
- Qdrant as a governed source
- Salesforce
- Notion
- Google Workspace

### Deferred Or Disabled

- SAML authentication.
- Semantic-cache serving.
- External notification delivery.
- Qdrant as the runtime cache/vector backend.
- Automatic category classification.
- Broad SaaS mutations.
- A native MySQL wire-protocol listener.

Deferred capabilities are not part of the V1 compatibility promise. An Admin
control for a deferred capability must be hidden, disabled, or labeled as
planned.

## Normative Documents

| Surface | Contract |
| --- | --- |
| Compatibility and deprecation | [compatibility-policy.md](/reference/contracts/compatibility-policy/) |
| MCP transport and tools | [mcp-v1.md](/reference/contracts/mcp-v1/) |
| MCP machine-readable schemas | [mcp-tools-v1.json](/contracts/mcp-tools-v1.json) |
| HTTP proxy | [http-proxy-v1.md](/reference/contracts/http-proxy-v1/) |
| PostgreSQL proxy | [postgresql-v1.md](/reference/contracts/postgresql-v1/) |
| MySQL/MariaDB connector | [mysql-connector-v1.md](/reference/contracts/mysql-connector-v1/) |
| Configuration | [configuration-v1.md](/reference/contracts/configuration-v1/) |
| Helm chart | [helm-v1.md](/reference/contracts/helm-v1/) |
| Database migrations | [migrations-v1.md](/reference/contracts/migrations-v1/) |
| Error behavior | [errors-v1.md](/reference/contracts/errors-v1/) |
| Release process | [release-candidate.md](/reference/contracts/release-candidate/) |

## Contract Precedence

For a released version, precedence is:

1. Security advisories for that version.
2. The tagged release notes and migration notes.
3. The V1 documents in this directory.
4. Examples, guides and concept pages elsewhere in this documentation.

Older design notes and dated certification reports are kept out of the
repository; where they are quoted in discussion they explain intent and prior
findings, and they never override this contract.

## Compatibility Rules

- Public JSON field names, MCP tool names, supported PostgreSQL behavior,
  documented error identifiers, Helm values, and migration history are
  compatibility surfaces.
- Additive optional fields are compatible. Clients MUST ignore unknown fields
  unless a schema explicitly sets `additionalProperties` to `false`.
- A security fix MAY tighten validation without a deprecation period when the
  previous behavior permits authorization bypass, secret disclosure, data
  loss, or unsafe egress.
- InterLock does not promise byte-for-byte error prose. Consumers SHOULD use
  HTTP status, JSON-RPC code, stable error identifier, or SQLSTATE.
- The Python import namespace is `interlock`. The `ONYX_*` environment prefix
  is a deprecated compatibility surface, honored with a warning until no
  earlier than `1.2.0`; see [compatibility-policy.md](/reference/contracts/compatibility-policy/).

## Evidence Required For Stable Claims

A stable surface requires focused unit tests, integration or compose coverage,
and release-candidate evidence tied to one commit. Cloud and live-source
certifications are recorded separately and never contain credentials or
private resource identifiers.
