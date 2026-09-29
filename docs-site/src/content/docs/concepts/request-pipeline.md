---
title: The request pipeline
description: The steps every agent request goes through, in order, and how the protocols differ.
sidebar:
  order: 2
---

Every request, on every protocol, goes through the same steps. A request that
fails a step stops there, and is still audited.

1. **Authenticate.** The API key (or PostgreSQL password, or OIDC token)
   identifies an agent identity. Unknown or disabled keys are refused.
2. **Classify.** SQL is parsed into statements. Each statement's tables and,
   where the catalog allows, columns become the resources the request touches;
   writes are classified by risk. HTTP requests are classified by method.
3. **Source roles.** The identity's grants on this source must allow every
   resource the request touches. Explicit deny wins. Nothing allowed means
   denied. See [Source roles](/concepts/source-roles/).
4. **Policy.** Enabled policy rules are checked in priority order; the first
   that matches decides. A rule can deny, or allow with redaction, a rate
   limit or a write-risk cap. A request no rule matches is denied. Policy never
   grants what roles did not. See [Policies](/concepts/policies/).
5. **Rate limit.** Per-identity limits, including any a policy set.
6. **Write approval.** A medium or high risk write is queued for a reviewer and
   the agent is told so; it is not executed. See
   [Write approval](/concepts/write-approval/).
7. **Cache.** A repeatable read may be answered from cache. Cache keys include
   the identity's scope, so one identity is never served another's answer.
8. **Upstream.** The request goes to the source.
9. **Redact.** The response is scanned for PII and, where a policy says so,
   named columns are redacted. See [Redaction and PII](/concepts/redaction-and-pii/).
10. **Audit.** One row records the identity, source, protocol, what was asked,
    the decisions, redaction counts and latency.

## Where the protocols differ

The steps are the same; a few capabilities are not available everywhere.

| | PostgreSQL wire | MCP | HTTP proxy |
|---|---|---|---|
| Authentication | API key as the password, or a dedicated PostgreSQL username and password | `Authorization: Bearer` API key | `Authorization: Bearer` API key |
| Source chosen by | the database name | the `source_id` argument | the path, `/proxy/{source_id}/...` |
| Resources checked | tables, and columns where the catalog resolves them | tables, and columns where the catalog resolves them | method and path |
| PII scanner redaction | yes | yes | JSON, text and CSV bodies |
| Policy `redact_columns` | **no** | yes | JSON bodies |
| Write held for approval | yes, the statement is refused with the approval id | yes, the tool result says it was queued | yes, `202` with the approval id |
| Cache | yes | yes | yes, for `GET` |

On the PostgreSQL wire only the PII scanner redacts. To keep a column from
leaving over it, deny the column rather than relying on redaction.

## Refusals an agent can read

A refused request says why: `Source role denied: ...`, `Policy denied: ...`,
`Unknown data source: ...`. Over MCP it arrives as a tool result with
`isError: true`, so an agent can explain rather than retry. The exact codes and
messages are in [Errors](/reference/errors/).
