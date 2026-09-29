---
title: "MCP V1 Transport And Tool Contract"
description: "The MCP transport, dialects and tools promised in V1."
normative: true
sidebar:
  order: 7
---

Status: stable release-candidate surface for `1.0.0-rc.1`.

## Transport

The canonical MCP endpoint is:

```text
POST /mcp
```

It uses JSON-RPC 2.0 over stateless Streamable HTTP.

### Protocol versions

The endpoint serves two dialects. Which one applies is decided per request.

| Dialect | Versions | How it is selected |
| --- | --- | --- |
| Modern | `2026-07-28` | `MCP-Protocol-Version: 2026-07-28` |
| Handshake | `2025-03-26`, `2025-06-18`, `2025-11-25` | Negotiated by `initialize`, or named in the header |

`server/discover` advertises `2026-07-28`; a client that does not know that
method negotiates through `initialize` instead.

An absent `MCP-Protocol-Version` header is not an error. Clients omit it on
`initialize` because no version has been agreed yet, and MCP 2025-06-18
instructs a server to assume `2025-03-26` when it is missing on later
requests. A header naming an unsupported version is rejected with `-32022`,
whose `data.supported` lists every version above.

Required in both dialects:

- `Content-Type: application/json`.
- `Accept` includes both `application/json` and `text/event-stream`.
- Requests are JSON objects, not JSON-RPC batches.
- Tool execution uses `Authorization: Bearer <agent-api-key>`.
- The request `Origin`, when present, must match the request origin or an
  operator-configured MCP origin allowlist.

Required in the modern dialect only:

- `Mcp-Method` mirrors the JSON-RPC method and `Mcp-Name` mirrors the tool name
  for `tools/call`.
- Every request includes `_meta.io.modelcontextprotocol/protocolVersion` and
  `_meta.io.modelcontextprotocol/clientCapabilities`.

These mirrors are a `2026-07-28` integrity feature and are not requested by any
handshake-era client, so requiring them there would make the endpoint
unreachable. They remain mandatory for clients that declare `2026-07-28`.

InterLock is stateless at this endpoint. It does not issue or require an MCP
session ID in either dialect; clients MUST NOT require one. `GET /mcp` and
`DELETE /mcp` are not supported and return `405`, which clients treat as
"no server-initiated stream".

InterLock may return one request-scoped SSE stream containing the JSON-RPC
response. Closing that stream cancels in-flight governed work. It does not
provide a server-owned standalone `GET` stream at the canonical endpoint.

## JSON-RPC Methods

| Method | Result | Dialect | Authentication |
| --- | --- | --- | --- |
| `server/discover` | Supported versions, capabilities, cache hints | Both | Not required |
| `initialize` | Negotiated version, `tools` capability, server info | Handshake | Not required |
| `notifications/initialized` | HTTP `202` with no body | Handshake | Not required |
| `ping` | Empty object | Handshake | Not required |
| `notifications/cancelled` | HTTP `202` with no body | Both | Not required |
| `tools/list` | Tool catalogue | Both | Not required |
| `tools/call` | MCP content result | Both | Required for governed tools |

`initialize` answers with the requested version when it is supported, and
otherwise with the latest handshake version InterLock supports, leaving the
client to decide whether to continue. Only the `tools` capability is
advertised: a client told about `resources` or `prompts` would call methods
this server answers with `-32601`.

Every successful result has `resultType: complete` and identifies InterLock in
`_meta.io.modelcontextprotocol/serverInfo`, with two deliberate exceptions:
`initialize` and `ping` return the plain shapes their specification defines,
because deployed clients validate those strictly and reject unknown keys. In
the modern dialect `initialize` and `ping` remain method-not-found. Clients
MUST not derive authorization from the tool catalogue; authorization is
evaluated again for each tool call.

### Tool results for refusals and queued writes

A governed refusal is not a transport failure, and a write held for approval is
not an error. Both are returned as tool results so the agent can read and
report them:

- **Queued write** - `isError: false`, with
  `structuredContent: {status: "queued", approval_id, approval_state, message}`
  and content text stating that the statement was not executed and must not be
  retried. `isError` is false because clients retry failed tool calls and the
  approval queue does not deduplicate, so a retry would enqueue a second
  approval for one intended write.
- **Refusal** (`401`, `403`, `429`) in the handshake dialect - `isError: true`,
  with `structuredContent: {status, code, message}` carrying the decision
  reason. In the modern dialect these remain JSON-RPC errors with the
  corresponding HTTP status, which is that dialect's contract.
- **Unavailable or unknown source** (`503`, `404`) in the handshake dialect -
  `isError: true`, with `structuredContent.status` of `unavailable` or
  `not_found` and the reason: the source's circuit is open, it is disabled, its
  upstream TLS was refused, or no source has that id. In the modern dialect
  these are JSON-RPC errors carrying the same reason (`-32603` for `503`,
  `-32602` for `404`).
- **Query error** (`422`) in the handshake dialect - `isError: true`, with
  `structuredContent.status` of `query_error` and text
  `Query error <code>: <message> (correlation ID <id>)`, when the database
  rejected the statement itself: SQLSTATE classes `42` (syntax, unknown table
  or column, permissions), `22` (bad data), `23` (integrity) and `0A`
  (unsupported), and the matching MySQL codes. Only the database's primary
  message is returned, never a DETAIL or HINT, which can quote row values or
  name columns the agent is not granted. In the modern dialect this is
  `-32602` with the same text. The legacy route returns `422` with
  `query_error: {category, code, message, correlation_id}`.
- **Any other execution failure** stays an opaque `tool execution failed`,
  because a raw upstream exception can carry addresses the error contract
  forbids returning. It carries the correlation ID of its audit event, so an
  administrator can find the full reason.

## Tool Result Envelope

Successful tools return the MCP content envelope:

```json
{
  "content": [
    {"type": "text", "text": "..."}
  ]
}
```

Structured source/query/discovery results are JSON-encoded inside the text
item in V1. Clients SHOULD parse the text as JSON for the tools documented
below. Additive fields may appear.

## Stable InterLock Tools

The exact input schemas are published in
[mcp-tools-v1.json](/contracts/mcp-tools-v1.json).

### `interlock_query`

Executes SQL against the registered `source_id`. SQL is parsed, source-role and
policy checked, write-classified, approval-gated where required, redacted,
cached when eligible, and audited. The result text is a JSON array of row
objects. It does not query the InterLock control database for registered source
data.

### `interlock_list_sources`

Returns a JSON object keyed by source ID. Only active sources visible through
the caller's current source-role grants are included.

### `interlock_describe_access`

Returns a JSON object keyed by source ID, describing what the caller's own
source roles allow and deny on each granted source. Each entry carries the
granted `roles` and the `allow` and `deny` statements as `action` and `resource`
pairs. An optional `source_id` narrows the answer; a source the caller holds no
grant on is omitted rather than reported as empty, so the tool never discloses
another identity's authority or the existence of an ungranted source.

Policy rules are evaluated per request and can still refuse an action listed
here, so an `allow` entry is permission to attempt rather than a guarantee of
success. The statements are read through the same evaluator that decides
enforcement, so the description cannot drift from the decision.

Requires an authenticated caller: without a resolved identity the tool refuses
with 403 rather than returning an empty object, which would read as "you may do
nothing".

Each source's entry also carries `resources`, built from the source catalog.
For a SQL source it is `{"enforced": true, "catalogued": true, "scanned_at",
"catalog_truncated", "tables": [...], "truncated"}`, where each table entry is
`{"table": "<schema>.<table>", "columns": [...], "all_columns": bool}` plus
`"policy": {"refused": true, "rule", "reason"}` when a policy would refuse it.
Only tables and columns the caller's roles allow and do not deny are listed;
nothing the caller cannot read is named. At most 200 tables are listed per
source. A source with no catalog yet reports `"catalogued": false`, and a
non-SQL source reports `"enforced": false`, meaning its requests are not
checked against catalogued resources.

### `interlock_describe_source`

Returns schema or connector introspection for one authorized source. Shape is
connector-specific and additive. For a catalogued PostgreSQL source the rows
come from the catalog, across every schema, as `{"table_schema",
"table_name", "column_name", "data_type"}`, and only for tables the caller may
describe; a caller whose roles name individual tables is no longer refused
outright. Other connectors return their own introspection.

### `interlock_discover`

Searches within one required source scope. Supported filters are source types,
document types, category path, time range, audience, entities, related-result
expansion, and a bounded result limit.

### `interlock_related_documents`

Returns documents related to one discovery asset. InterLock resolves the seed
asset's source and prevents cross-source expansion.

## Governance And Errors

Tool execution follows identity resolution, source resolution, source-role
authorization, policy, rate limit, write safety, execution, redaction, and
canonical audit. A policy cannot grant source access that a source role did
not allow.

JSON-RPC error codes:

| Code | Meaning |
| ---: | --- |
| `-32700` | Invalid JSON |
| `-32600` | Invalid request, media type, protocol version, or headers |
| `-32601` | Method not found |
| `-32602` | Invalid tool parameters or governed client-visible tool failure |
| `-32603` | Internal tool failure |
| `-32020` | Required mirrored header or request metadata mismatch |
| `-32022` | Unsupported protocol version |

HTTP status remains meaningful for transport and authorization failures. See
[errors-v1.md](/reference/contracts/errors-v1/). Error prose is not a stable machine key.

## Legacy Compatibility

The five equivalent `agentgate_*` tool names and the custom routes
`/mcp/tools/list`, `/mcp/tools/call`, and `/mcp/sse` are deprecated. Their
earliest removal is `1.2.0` under the one-minor-release policy.

The custom routes preserve the previous InterLock integration surface. The
canonical endpoint now negotiates pre-2026 MCP lifecycle through `initialize`,
so a handshake-era client should use `POST /mcp` rather than these routes.
`/mcp/tools/call` returns `approval_id` and `approval_state` alongside `error`
for a queued write, matching the HTTP proxy.
