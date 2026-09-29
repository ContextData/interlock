---
title: Upstream TLS
description: Configure verified TLS between InterLock and a PostgreSQL source.
sidebar:
  order: 3
---

In production, a PostgreSQL source is served only over TLS that verifies the
server's certificate:

- `sslmode` must be `verify-full` (recommended) or `verify-ca`.
- The CA certificate is given as `ssl_ca` (a path) or `ssl_ca_ref` (a secret
  reference); `sslrootcert` is accepted as an alias.

Anything weaker (`disable`, `prefer`, `require`, or `verify_ssl: false`) is
refused on every protocol. `require` is refused because it encrypts without
checking who is on the other end. The refusal happens at save and at Test
Connection, with the message `Verified upstream PostgreSQL TLS is required`, so a
source cannot be stored in a state the gateway would refuse.

The CA file must exist inside the gateway, admin and worker containers. With the
Helm chart, mount it from a Secret and give its path. Development deployments are
not held to this, so a local database without TLS keeps working.
