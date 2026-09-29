---
title: Cache
description: How InterLock caches repeatable reads without serving one agent another's answer.
sidebar:
  order: 14
---

InterLock can answer a repeated read from cache instead of the source. There are
two tiers: a small in-process cache in each gateway, and a shared Redis cache.

## What makes a cache key

A cached answer is only reused for the same question asked with the same
authority. The key includes the source, the normalised statement or path, the
identity, its mapped database role and team, the version of its grants, the
governance decisions that shaped the answer, the source's write generation,
and the protocol that asked. A change to any of these is a miss, never a wrong
hit.

The protocol matters because each one caches its own format: the PostgreSQL
wire protocol caches the bytes it sends to the client, and MCP caches the JSON
it returns to the agent. The same statement over both is two entries. Each
also checks that a cached entry is in its own format before serving it, so an
entry it cannot read is treated as a miss.

## Strategies

Each source chooses a strategy:

| Strategy | Behaviour |
|---|---|
| `deterministic_first` | cache exact repeats (the default for SQL sources) |
| `deterministic_only` | the same |
| `semantic_first`, `semantic_only` | accepted, but semantic serving is disabled, so only exact repeats are served |
| `bypass` | never cache this source |

## Staying correct

- A write through InterLock advances the source's generation and invalidates
  the affected entries on every gateway.
- Editing, disabling or deleting a source clears its cache. A disabled source
  answers nothing, cached or not.
- If the generation barrier cannot be checked, strict mode
  (`cache.strict_write_barrier`, the default) refuses to serve from cache
  rather than risk a stale answer.
- Writes made directly to the source, not through InterLock, are not seen:
  entries expire after their TTL (five minutes in Redis by default). Use
  `bypass` for sources other systems write to often, or invalidate from the
  source's page.
