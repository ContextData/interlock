---
title: MCP clients
description: Connect Claude Code, Codex or any MCP client to the gateway's /mcp endpoint.
sidebar:
  order: 1
---

The gateway serves MCP over streamable HTTP at `POST /mcp`. Clients authenticate
with the identity's API key in the `Authorization` header and need nothing
InterLock-specific: they negotiate with `initialize` as usual.

## Claude Code

```bash
claude mcp add --transport http interlock "$GATEWAY/mcp" \
  --header "Authorization: Bearer $API_KEY"
claude mcp list
```

## Other clients

Point the client at `$GATEWAY/mcp` with the same header. The gateway accepts
MCP protocol versions `2025-03-26`, `2025-06-18` and `2025-11-25` through the
usual `initialize` handshake, and the `2026-07-28` dialect, which instead
requires the protocol version and mirror headers on every request. A client
that sends an unsupported version gets `-32022` with the supported list.

The gateway does not issue an `Mcp-Session-Id`, does not open a server-sent
event stream on `GET /mcp`, and advertises tools only (no resources or
prompts).

## The tools

| Tool | Use |
|---|---|
| `interlock_list_sources` | the sources this identity is granted |
| `interlock_describe_access` | the tables, columns and actions it may use on each |
| `interlock_describe_source` | a source's structure, filtered to what it may see |
| `interlock_query` | run a governed SQL query or connector request |
| `interlock_discover` | search indexed content across granted sources |
| `interlock_related_documents` | find documents related to a discovery result |

Arguments are in [MCP tools](/reference/mcp-tools/). The `agentgate_*` names
are deprecated aliases, removed no earlier than `1.2.0`.

## How refusals and queued writes arrive

- A **refusal** (source role, policy, rate limit, unknown or unavailable
  source) is a tool result with `isError: true`; its text is the reason and
  `structuredContent.status` says which kind. Agents can explain it.
- A **write held for approval** is a tool result with `isError: false` whose
  text says the statement was not executed and must not be retried, with
  `structuredContent.approval_id`. Retrying would queue a second approval.

With the `2026-07-28` dialect, refusals are JSON-RPC errors instead, as that
dialect's contract requires. The full contract is
[MCP V1](/reference/contracts/mcp-v1/).
