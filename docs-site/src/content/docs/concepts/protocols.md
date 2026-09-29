---
title: Protocols
description: The three ways an agent connects to InterLock, and when to use each.
sidebar:
  order: 3
---

The gateway speaks three protocols. All three are governed by the same
[pipeline](/concepts/request-pipeline/); pick whichever the agent already
speaks.

## MCP

The Model Context Protocol, over streamable HTTP at `POST /mcp`. Agents built
on Claude Code and most MCP clients connect here, and call tools such as
`interlock_query`, `interlock_list_sources`, `interlock_describe_access` and
`interlock_discover`. The gateway accepts the 2025 MCP protocol versions used
by current clients, with an `initialize` handshake, and the 2026-07-28 dialect
with its required mirror headers. See [MCP tools](/reference/mcp-tools/) and
the [MCP contract](/reference/contracts/mcp-v1/).

## PostgreSQL wire

A PostgreSQL client (psql, a driver, an ORM) connects to the gateway as if it
were a PostgreSQL server. The **database name is the source ID**, and the
**API key is the password** (any username), unless the identity has a dedicated
PostgreSQL username and password. The gateway supports the simple and extended
query protocols; `COPY`, cancellation of another session, and GSS encryption
are not supported. In production the listener requires TLS. Only asyncpg is
certified as a client today. See the
[PostgreSQL contract](/reference/contracts/postgresql-v1/).

## HTTP

For HTTP sources (the `generic_rest` connector), the gateway is a reverse
proxy: `/proxy/{source_id}/{path}` forwards to the source's base URL with the
agent's request governed by method and path. `GET` and `HEAD` are reads; other
methods are writes. See the [HTTP proxy contract](/reference/contracts/http-proxy-v1/).
