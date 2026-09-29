---
title: "V1 Error Contract"
description: "The error codes and shapes each protocol returns in V1."
normative: true
sidebar:
  order: 4
---

Status: stable release-candidate surface.

InterLock errors are safe, protocol-native, and correlated with audit/log
records. Raw upstream exceptions, credentials, tokens, cookies, SQL literals
in non-owner views, private addresses, and stack traces must not be returned to
agents.

## Stable Machine Signals

Consumers should branch on:

- HTTP status and stable `error` identifier where present.
- JSON-RPC error `code` for MCP.
- PostgreSQL SQLSTATE.
- Approval `approval_state` and identifier.
- Correlation ID when supplied for operator support.

Human-readable message text and punctuation are not stable.

## Canonical Failure Classes

| Failure | HTTP | MCP JSON-RPC | PostgreSQL | Retry guidance |
| --- | ---: | ---: | --- | --- |
| Invalid input | `400`/`422` | `-32600`/`-32602` | `42601` or proxy-specific error | Correct request |
| Missing/invalid auth | `401` | handshake: `isError` result, `status: unauthenticated`; modern: `401` transport | `28000` | Refresh credential |
| Source-role/policy deny | `403` | handshake: `isError` result, `status: denied`; modern: `403` transport | `42501` | Do not retry unchanged |
| Egress blocked | `403` | tool error | n/a | Operator action required |
| Approval pending | `202` | tool result, `isError: false`, `structuredContent.approval_id` | `42501`-class governed response | Poll Admin/workflow, never retry - the queue does not deduplicate |
| Rate limit | `429` | handshake: `isError` result, `status: rate_limited`; modern: `429` transport | `53400` | Retry with backoff |
| Body/result limit | `413` | invalid request/tool error | `54000` | Reduce request/result |
| Unknown source/resource | `404` | handshake: `isError` result, `status: not_found`; modern: `-32602` with the reason | `3D000` (unknown database) / `42501` | Correct source or grant |
| Source unavailable (circuit open, disabled, upstream TLS refused) | `503` on `/mcp/tools/call` | handshake: `isError` result, `status: unavailable`, with the reason; modern: `-32603` with the reason | FATAL `08004` with the reason | Operator action; back off while a circuit is open |
| Upstream failure | `502` | `-32603` or sanitized tool failure | upstream/proxy SQLSTATE; FATAL `08006` when the connection cannot be opened, detail logged only | Retry only if operation is idempotent |
| Mandatory dependency unavailable | `503` | `-32603` or tool `503` | `08004`/connection close | Backoff; check readiness |
| Audit unavailable in strict mode | `503` | tool `503` | operation rejected | Do not bypass audit |
| Cache barrier unavailable for write | `503` | tool `503` | operation rejected | Retry after Redis recovery |
| Unsupported PG feature | n/a | n/a | `0A000` | Use documented subset |

## Error Identifiers

Identifiers currently intended for machine use include:

- `egress_blocked`
- `cache_consistency_unavailable`
- `invalid_json`
- `request_too_large`
- `response_too_large`
- `upstream_request_failed`

New identifiers may be added. Renaming or removing a published identifier
follows the deprecation policy unless required to close an information leak.

## Write Retry Safety

A client must not automatically replay a write after a timeout, `502`, or
`503` unless it supplied a source-supported idempotency key or independently
verified the operation did not execute. Approval IDs are stable workflow
identifiers, not generic idempotency keys.

## Audit Correlation

Denied and failed governed requests are audited when the durable audit path is
available. If strict audit cannot accept the event, the request is rejected.
Operators use the correlation ID to join client symptoms, structured logs,
approval state, traces, and audit records.
