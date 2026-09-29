---
title: Troubleshooting
description: The errors operators and agents see most often, what they mean, and what to do.
sidebar:
  order: 99
---

| Message | Where | Meaning and fix |
|---|---|---|
| `Missing or invalid Authorization header` | HTTP, MCP | No `Authorization: Bearer <key>` header. |
| `Invalid API key` | HTTP, MCP | The key is wrong, or stored in a form this deployment refuses (production refuses legacy SHA-256 hashes). Rotate the key. |
| `Unknown or disabled API key` | HTTP, MCP | The identity is disabled or deleted. |
| `Invalid InterLock PostgreSQL credentials` (`28P01`) | PostgreSQL | The password is not a valid key, and not the identity's dedicated PostgreSQL password. |
| `PostgreSQL client TLS is required` (`28000`) | PostgreSQL | Production requires TLS: connect with `sslmode=require` or stronger. |
| `Unknown data source: <id>` (`3D000` on PostgreSQL, `404` elsewhere) | all | No enabled source has that ID: check the database name or `source_id`, and whether the source was disabled. |
| `Source role denied: ...` | all | The identity's roles on this source do not allow every resource the request touched. Check its grants and the role's statements; the role dry-run shows why. |
| `Policy denied: ...` | all | A policy rule denied it, or no rule matched. The message names the rule. |
| `... risk write requires approval` or a returned `approval_id` | all | The write was queued, not run. Review it under **Write Safety**. |
| `Verified upstream PostgreSQL TLS is required` | admin, all | Production PostgreSQL sources need `sslmode` `verify-full` or `verify-ca` and a CA. See [Upstream TLS](/guides/manage-sources/upstream-tls/). |
| `Private or local egress target is blocked` | admin | The source's host is on a private network. Set `"allow_private_egress": true` on the source if that is intended. |
| `Connector ... is not active` | admin | Activate the connector on **Connectors** first. |
| `Upstream PostgreSQL connection failed` (`08006`) | PostgreSQL | The gateway could not reach the source. Check the source's host and credentials with **Test Connection**. |
| `Circuit open for data source: <id>` | all | Repeated upstream failures; the gateway stops trying for a short while. Fix the upstream and it recovers. |
| `password_change_required` | admin API | The admin account must change its default password first. |
| `Invalid or missing CSRF token` | admin API | Send the token from `GET /auth/csrf` in `X-CSRF-Token` on every `POST`, `PUT`, `PATCH` and `DELETE`. |

Every code and response shape is specified in the
[error contract](/reference/contracts/errors-v1/).
