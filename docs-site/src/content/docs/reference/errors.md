---
title: Errors
description: How each protocol reports a refusal or failure, and where the exact codes are specified.
sidebar:
  order: 3
---

Every refusal says why, in a form the client understands:

| Protocol | Refusal | Unknown or withdrawn source | Upstream unavailable |
|---|---|---|---|
| MCP, 2025 dialects | tool result, `isError: true`, `structuredContent.status` of `denied`, `rate_limited`, `unauthenticated` | tool result, `isError: true`, status `not_found` | tool result, `isError: true`, status `unavailable` |
| MCP, `2026-07-28` | JSON-RPC error | JSON-RPC error | JSON-RPC error `-32603` with the reason |
| PostgreSQL wire | `ERROR` `42501` with the reason; authentication `28P01` or `28000` | `3D000` | `FATAL` `08004` with the reason, or `08006` |
| HTTP proxy | `401`, `403` or `429` with a JSON `error` | `404` | `502` or `503` |

A write held for approval is not an error: MCP returns `isError: false` with an
`approval_id`, HTTP returns `202` with it, and the PostgreSQL wire refuses the
statement with the id in the message.

Internal failures never return upstream addresses or credentials; the detail
stays in the gateway log.

The normative definitions are in the [error contract](/reference/contracts/errors-v1/).
The messages operators meet most often are explained in
[Troubleshooting](/guides/troubleshooting/).
