---
title: Production checklist
description: What must be true before InterLock serves real agents and real data.
sidebar:
  order: 1
---

InterLock refuses to start in production (`environment: production`, which the
Helm chart sets) when the settings marked **enforced** are missing. The rest are
yours to check.

## Enforced at start

- `admin.secret_key`: 32+ random characters.
- `auth.api_key_pepper`: 32+ random characters. Choose it once; changing it
  invalidates every API key.
- `auth.allow_legacy_sha256_keys: false`.
- `admin.cookie_secure: true`, with the console behind HTTPS.
- `database.ssl_mode: verify-full` with the control database's CA.
- `audit.durability_mode: strict`, with a writable spool volume.
- TLS on the gateway's PostgreSQL listener.
- Verified TLS to every PostgreSQL source.

## Before anyone else can reach it

- Sign in and change the default `admin` password, or set the first password
  with `admin.bootstrap_password` and remove it afterwards.
- Restrict who can reach the console: a private network, an allow-list or an
  authenticating proxy. It is an administrative surface.
- Give each source a database login with only the privileges its roles
  describe. This is the real limit on what agents can reach; see
  [Security model](/concepts/security-model/).
- Store every source credential as a secret reference, never inline.
- Create at least one `allow` policy rule; without one every request is denied.
- Check the [connector support matrix](/reference/connector-support-matrix/)
  for each connector you activate.

## Keep running

- Back up the control database; see [Backup and restore](/operations/runbooks/backup-restore/).
- Watch `/ready` on every service and the audit spool volume.
- Follow [Upgrades and migrations](/operations/upgrades-and-migrations/) for
  every new release.
