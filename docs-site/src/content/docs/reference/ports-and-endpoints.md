---
title: Ports and endpoints
description: What each service listens on, in the container and on the local Compose stack.
sidebar:
  order: 4
---

| Service | Container port | Local Compose (`127.0.0.1`) | Serves |
|---|---|---|---|
| Gateway, HTTP | `3000` | `3001` | `/mcp`, `/proxy/{source_id}/...`, `/health`, `/ready`, `/stats` |
| Gateway, PostgreSQL | `5432` | `5434` | the PostgreSQL wire listener |
| Admin | `9090` | `9090` | the console, `/api/...`, `/auth/...`, `/health`, `/ready` |
| PostgreSQL (InterLock's own) | `5432` | `5433` | the control database |
| Redis | `6379` | `6380` | cache, sessions, coordination |

The worker listens on nothing.

## Health

- `GET /health` answers `200` while the process is up.
- `GET /ready` answers `200` only when every dependency check passes
  (database, migrations at head, Redis, registry, audit buffer, cache
  invalidation), with each check in the body; otherwise `503`. Use it for
  readiness probes.
- `GET /stats` on the gateway reports cache and connection statistics. It needs
  authentication unless `observability.public_stats_enabled` is set.

## MCP routes

`POST /mcp` is the V1 endpoint. `POST /mcp/tools/list`, `POST /mcp/tools/call`
and `GET /mcp/sse` are the legacy routes, deprecated with the `agentgate_*`
tool names.
