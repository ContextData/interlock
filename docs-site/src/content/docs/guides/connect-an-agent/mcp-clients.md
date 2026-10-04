---
title: MCP clients
description: Connect Claude Code, Codex, the OpenAI, Gemini and Anthropic APIs, or any MCP client to the gateway's /mcp endpoint.
sidebar:
  order: 1
---

The gateway serves MCP over streamable HTTP at `POST /mcp`. Clients authenticate
with the identity's API key in the `Authorization` header and need nothing
InterLock-specific: they negotiate with `initialize` as usual. InterLock
governs the key, not the model: any client that presents an identity's key gets
exactly that identity's source roles, policies, redaction and audit.

There are two kinds of client:

- **Local clients** (Claude Code, Codex CLI, an SDK in your own code) connect
  from where they run, so they can reach a gateway on a private network.
- **Hosted connectors** (the OpenAI Responses API, the Gemini Interactions API,
  the Anthropic API's MCP connector) connect from the provider's servers. The
  gateway must be reachable over public HTTPS, and the key is sent to that
  provider with every request. Give each one its own identity with the
  narrowest role that works, and rotate its key if it is exposed.

## Claude Code

```bash
claude mcp add --transport http interlock "$GATEWAY/mcp" \
  --header "Authorization: Bearer $API_KEY"
claude mcp list
```

## Codex CLI

```bash
export INTERLOCK_API_KEY=...   # the identity's key
codex mcp add interlock --url "$GATEWAY/mcp" --bearer-token-env-var INTERLOCK_API_KEY
```

Codex reads the key from the named variable and never writes it to its config.
It asks for approval before every InterLock tool call, because the tools do not
declare MCP annotations. For unattended use (`codex exec`), set
`default_tools_approval_mode = "approve"` under `[mcp_servers.interlock]` in
`config.toml`; with `auto`, calls are still held for approval and fail when no
one can give it.

## OpenAI Responses API

```python
response = client.responses.create(
    model="gpt-5.5",
    input="...",
    tools=[{
        "type": "mcp",
        "server_label": "interlock",
        "server_url": f"{GATEWAY}/mcp",
        "headers": {"Authorization": f"Bearer {API_KEY}"},
        "require_approval": "never",
    }],
)
```

Calls appear as `mcp_call` output items. A refusal arrives as an
`McpToolExecutionError` carrying InterLock's reason.

## Gemini

Through the Interactions API, Google's side connects to the gateway:

```python
interaction = client.interactions.create(
    agent="antigravity-preview-09-2026",
    input="...",
    environment="remote",
    tools=[{"type": "mcp_server", "name": "interlock", "url": f"{GATEWAY}/mcp",
            "headers": {"Authorization": f"Bearer {API_KEY}"}}],
)
```

The interaction object Google stores and returns includes the headers you
sent, the key among them. Prefer the Interactions API's named credentials over
inline headers for anything long-lived.

Through the `google-genai` SDK, your code holds the MCP session and the SDK
calls InterLock's tools from your machine:

```python
async with streamablehttp_client(f"{GATEWAY}/mcp", headers={"Authorization": f"Bearer {API_KEY}"}) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        response = await client.aio.models.generate_content(
            model="gemini-3.8-flash", contents="...",
            config={"tools": [session]},
        )
```

Pass `config` as a dict. A `GenerateContentConfig` object is deep-copied by the
SDK, which fails on a live MCP session (`cannot pickle '_asyncio.Future'`). With
version 2 of the `mcp` package, use `streamable_http_client(url,
http_client=create_mcp_http_client(headers=...))` instead.

## Anthropic API

The Messages API's MCP connector connects from Anthropic's side:

```python
response = client.beta.messages.create(
    model="claude-opus-5-5", max_tokens=16000,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[{"type": "url", "url": f"{GATEWAY}/mcp", "name": "interlock",
                  "authorization_token": API_KEY}],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "interlock"}],
    messages=[...],
)
```

The connector negotiates the `2026-07-28` dialect, in which InterLock returns
source-role and policy refusals as HTTP 403. The connector reports those to
the model as `Connection closed`, without InterLock's reason; the refusal
itself is enforced and audited. Query errors (HTTP 422) do arrive with their
detail.

## Other clients

Point the client at `$GATEWAY/mcp` with the same header. The gateway accepts
MCP protocol versions `2025-03-26`, `2025-06-18` and `2025-11-25` through the
usual `initialize` handshake, and the `2026-07-28` dialect, which instead
requires the protocol version and mirror headers on every request. A client
that sends an unsupported version gets `-32022` with the supported list.

The gateway does not issue an `Mcp-Session-Id`, does not open a server-sent
event stream on `GET /mcp`, and advertises tools only (no resources or
prompts). It accepts requests with no `Origin` header, which is what
server-to-server connectors send; browser origins must be listed in its
configuration.

## Tested clients

Each client was given the same five tool calls against a PostgreSQL source
through a dedicated identity, and checked against what the provider recorded
and against InterLock's audit log: the granted sources listed, a read returned
with emails redacted, a `DELETE` refused by the source role, a table refused by
a deny policy, a missing column reported with SQLSTATE `42703`, and one audit
row per call.

| Client | Version | Route | Date | Result |
|---|---|---|---|---|
| Claude Code | | local | 2026-09 | Used throughout the DigitalOcean rehearsals |
| Official MCP Python SDK | `mcp` 2.0 | local | every CI run | `make test-mcp-sdk` |
| Codex CLI | 0.160.0 | local | 2026-10-04 | All checks pass with `default_tools_approval_mode = "approve"` |
| OpenAI Responses API | `gpt-5.5`, `openai` 3.24.0 | hosted | 2026-10-04 | All checks pass |
| Gemini Interactions API | `antigravity-preview-09-2026` | hosted | 2026-10-04 | All checks pass |
| `google-genai` SDK | 2.28.0, `gemini-3.8-flash`, `mcp` 1.30 and 2.0 | local | 2026-10-04 | All checks pass with `config` as a dict |
| Anthropic API MCP connector | `anthropic` 1.11.0 | hosted | 2026-10-04 | Reads, query errors and audit pass; refusals reach the model without their reason (see above) |

The same pass was repeated on 2026-10-04 against
[an Amazon EKS deployment without a domain](/operations/deploy-on-amazon-eks/),
whose gateway presents a certificate from a private CA. Codex CLI (trusting it
through `CODEX_CA_CERTIFICATE`) and the `google-genai` SDK (through
`SSL_CERT_FILE`) passed every check. The three hosted connectors were not run
there: they connect only to a gateway with a publicly trusted certificate.

In the Anthropic run, Claude Opus 5.5 declined the test prompt under its
`cyber` safety classifier, and the request was answered by Claude Opus 4.8
through the API's server-side fallback (`fallbacks: "default"`). The connector
behaviour is the same for either model.

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
are deprecated aliases, removed no earlier than `1.2.0`; they are still listed,
so clients see eleven tools.

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
