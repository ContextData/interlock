---
title: Security hardening
description: Steps beyond the enforced defaults that reduce what a compromise could reach.
sidebar:
  order: 5
---

- **Least-privileged upstream logins.** Each source's login should hold only the
  privileges its roles describe. InterLock's SQL governance cannot see inside
  views, functions or dynamic SQL; the database's own grants can.
- **Keep the console private.** Put it on a private network, behind an
  allow-list or an authenticating proxy. Use OIDC for admins where you can.
- **Separate identities.** One per agent deployment, so a leaked key is revoked
  alone and audit rows are attributable.
- **Deny, don't only redact.** A column that must never leave should be denied;
  redaction depends on the protocol and on recognising a value's shape.
- **Rotate keys** on a schedule and when people leave; rotation drops the old
  key's session at once.
- **Restrict egress.** Leave `allow_private_egress` off except for sources that
  need it, and control the gateway's outbound network where you can.
- **Protect the spool and the database.** The audit spool and control database
  hold statements and decisions; encrypt their volumes and restrict access.
- **Watch the admin audit log** for connector activation, role and policy
  changes, and password changes.

Report vulnerabilities privately: [Security policy](/project/security-policy/).
