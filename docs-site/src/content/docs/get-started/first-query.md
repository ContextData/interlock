---
title: First governed query from Claude Code
description: Connect Claude Code to InterLock over MCP and query a governed source.
sidebar:
  order: 3
---

This connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to
the gateway you started in the [quick start](/get-started/quick-start/), using
the agent key you created there.

## Register the gateway as an MCP server

```bash
claude mcp add --transport http interlock http://127.0.0.1:3001/mcp \
  --header "Authorization: Bearer $API_KEY"
claude mcp list
```

`claude mcp list` should show `interlock` as connected. The key travels only in
the `Authorization` header; keep it out of prompts and files.

## Ask for data

Start Claude Code and ask, for example:

> List the sources you can use, then show me the three most recent orders with
> the customer's name and email.

Claude Code calls `interlock_list_sources`, then `interlock_query` against
`sample_shop`. The emails come back as `[REDACTED:EMAIL]`: the agent never sees
them.

Ask it to delete an order. The tool result says the request was denied by the
source role, and Claude Code reports that instead of retrying.

## What the agent can learn about its own access

`interlock_describe_access` tells an agent which sources, tables and columns it
may use, and which a policy would refuse, without it having to probe. See
[MCP tools](/reference/mcp-tools/) for every tool.

## Other clients

Any MCP client that speaks the streamable HTTP transport connects the same way;
[Connect an agent](/guides/connect-an-agent/mcp-clients/) covers the details.
PostgreSQL clients connect with the database name set to the source ID and the
API key as the password, as the quick start's `psql` example shows.
