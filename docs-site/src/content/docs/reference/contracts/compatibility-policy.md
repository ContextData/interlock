---
title: "V1 Compatibility And Deprecation Policy"
description: "How V1 surfaces change, how deprecations work, and which identifiers never change."
normative: true
sidebar:
  order: 2
---

Status: normative for InterLock `1.x` releases.

## Semantic Versioning

InterLock uses Semantic Versioning for public releases.

- Patch releases (`1.0.x`) contain compatible fixes, security hardening, and
  additive optional behavior.
- Minor releases (`1.x.0`) may add compatible protocols, fields, tools,
  connectors, and configuration.
- Major releases may remove or incompatibly change a public contract.

Security and data-integrity fixes may reject input that was previously
accepted when accepting it would violate a documented security invariant.

## One-Minor-Release Deprecation Rule

A public V1 surface scheduled for removal MUST:

1. Be marked deprecated in documentation and release notes.
2. Remain operational for the next minor release.
3. Emit an operator-visible warning or response metadata where the protocol
   permits it without leaking sensitive data.
4. Be removed no earlier than the following minor release.

Example: a surface deprecated in `1.0` remains available in `1.1` and may be
removed in `1.2`.

The rule does not require continued support for a known exploit, compromised
dependency, provider API removed by its owner, or behavior that can corrupt or
expose data. Such exceptions require a security advisory and migration path.

## Current Deprecations

- MCP tools named `agentgate_*` are deprecated in `1.0.0-rc.1`. Use the
  corresponding `interlock_*` tools. They remain available through `1.1.x`
  and may be removed in `1.2.0`.
- Legacy MCP routes `/mcp/tools/list`, `/mcp/tools/call`, and `/mcp/sse` are
  deprecated in `1.0.0-rc.1`. Use `POST /mcp`. They follow the same removal
  window.
- `ONYX_*` environment variables are deprecated in `1.0.0-rc.1`, following the
  rename from Onyx to InterLock. Each is mapped onto the matching
  `INTERLOCK_*` variable at startup and emits a `DeprecationWarning` naming its
  replacement. `ONYX_CONFIG_PATH` is honored the same way. When both names are
  present the `INTERLOCK_*` value wins and the legacy one is ignored, so a
  partially migrated deployment resolves to the new name rather than a stale
  one. They remain available through `1.1.x` and may be removed in `1.2.0`.
- The Python import namespace is `interlock`. The previous `onyx` namespace is
  removed, not deprecated: it was never published to an index, so no released
  artifact imported it.

## Identifiers That Do Not Change

The rename deliberately stops at the data layer. These keep their original
names because they exist inside already-provisioned databases, and renaming
them would require destructive migration for no functional gain:

- PostgreSQL `LISTEN`/`NOTIFY` channels: `onyx_config_changed`,
  `onyx_policy_changed`, `onyx_write_approval`, `onyx_identity_changed`.
- Redis pub/sub channels: `onyx:faiss_rebuild`, `onyx:cache_invalidate`.
  Renaming either family would split coordination during a rolling upgrade,
  because old and new pods must agree on the channel name to see each other's
  invalidation and reload events.
- Redis keys: `onyx:cache:source_generation:` (the default source-generation
  barrier prefix; deployments set `cache.source_generation_prefix`),
  `onyx:faiss:generation:` and `onyx:gateway:instance:`. Renaming them would
  strand live counters and instance registrations across an upgrade.
- The Qdrant collection name default `onyx_cache`, and the OPA policy path
  default `v1/data/onyx/authz`, both of which name objects operators have
  already created.
- The secret-file directory `/etc/onyx/secrets`, still accepted by `file://`
  references beside `/etc/interlock/secrets` so existing mounts keep resolving.
- The `agentgate_seed_source_role_permissions` function from migration 007.
- The default PostgreSQL role and database names used by the reference compose
  stack (`onyx`). Operators choose their own in any real deployment.

Migration files are content-addressed. `verify_migrations` compares each
bundled file's checksum against the digest recorded when it was applied, so
editing a historical migration - even its comments - makes every already
migrated database fail readiness. Historical migrations still refer to earlier
product names in comments, and that is intentional.

## Stable And Non-Stable Surfaces

Stable surfaces are listed in [README.md](/reference/contracts/). Beta connector-specific
objects may gain additive fields and stricter validation in minor releases.
Deferred features have no compatibility guarantee until promoted to beta or
stable in a release note.

Internal implementation details are not public contracts, including:

- Python module layout under `src/interlock`.
- Private database helper functions.
- Redis key layout, except when explicitly documented for operators.
- HTML class names and internal HTMX fragment routes.
- Test fixture identities, source IDs, and seeded records.

## Database And Helm Compatibility

- Database migrations are forward-only and immutable after release.
- A patch or minor release MUST support upgrading from the latest patch of the
  prior supported minor release.
- Helm values documented in [helm-v1.md](/reference/contracts/helm-v1/) follow this deprecation
  policy. New required values may not be introduced in a patch release unless
  required to close a security vulnerability.

## Client Responsibilities

Clients SHOULD pin an InterLock minor version, tolerate additive JSON fields,
send explicit content types and protocol versions, and test against a release
candidate before production upgrade.
