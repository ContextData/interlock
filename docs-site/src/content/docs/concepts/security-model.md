---
title: Security model
description: What InterLock protects, the controls it relies on, and the limits of those controls.
sidebar:
  order: 16
---

## What it protects

- The data in your sources, against agents reaching more than they are allowed.
- Upstream credentials and secret references.
- Agent identities, keys, grants and policies.
- The audit trail, as evidence of what happened.

## The threats it is designed against

- An agent, or whoever controls it, bypassing source roles, policy, write
  approval, redaction, cache scoping or audit.
- Server-side request forgery through HTTP sources and connector probes.
- Secrets leaking through the console, logs, audit rows, approval records,
  connector errors or screenshots.
- Loss of audit records.
- One identity or source seeing another's cached answers or discovery results.

## Controls it relies on

- Source roles authorize access; policies can only restrict. A request nothing
  allows is denied, and explicit deny wins.
- Every protocol goes through the same pipeline; there is no ungoverned path to
  a source.
- Writes are classified before execution; risky ones wait for a person.
- Cache keys carry the identity's full scope and the source's write generation.
- Outbound connections are checked: private, loopback and cloud-metadata
  addresses are refused unless a source explicitly allows private egress.
- Secrets are stored as references and masked wherever they are shown.
- Production configuration is enforced at start: a signing key and API-key
  pepper of 32+ characters, no legacy key hashes, secure cookies, verified TLS
  to the control database and to PostgreSQL sources, TLS on the PostgreSQL
  listener, and strict audit durability.
- Admin actions are role-checked, CSRF-protected and audited.

## Limits

SQL governance works by parsing what the agent sends. It sees the statement,
not what the database does when running it:

- A view, a function, a trigger or a rule can read or write tables and columns
  the statement never names.
- Dynamic SQL built inside the database, and extension functions InterLock does
  not already treat as opaque reads, are invisible to the parser.
- The catalog records what the scanning login can see; a mapped role with a
  different search path is covered for denies, not for allows.
- Policy `redact_columns` is not applied on the PostgreSQL wire.
- The PII scanner recognises shapes; it will miss sensitive values that do not
  look like any it knows.

**The guarantee is the upstream grant.** Give the database login each source
uses only the privileges its roles describe, so that a statement InterLock
cannot see through still cannot reach data that login was never granted.

## Reporting a vulnerability

Report privately, never in a public issue: see the
[security policy](/project/security-policy/).
