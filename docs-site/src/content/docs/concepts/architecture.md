---
title: Architecture
description: The services InterLock runs, what each one does, and how they share state.
sidebar:
  order: 1
---

InterLock is three services over a shared PostgreSQL database and Redis. They
never call each other; everything they share goes through those two stores, so
each can be restarted, scaled or upgraded on its own.

| Service | Job | Listens on |
|---|---|---|
| **Gateway** | The data plane. Accepts agent connections, governs each request, forwards it upstream, redacts the response, writes the audit row. | HTTP and MCP on `3000`, PostgreSQL wire on `5432` |
| **Admin** | The control plane. The console and its JSON API: sources, connectors, roles, identities, policies, approvals, audit and usage. | `9090` |
| **Worker** | Background work: catalog scans, discovery ingestion (extraction, summaries, embeddings), scheduled refreshes. | nothing inbound |
| **PostgreSQL** | InterLock's own database: configuration, grants, policies, the audit log, the approval queue, the catalog and discovery index. Not your data. | `5432` |
| **Redis** | Response cache, sessions, CSRF tokens, rate-limit counters, worker heartbeats, and the channels that tell every gateway when to reload or invalidate. | `6379` |

Your data stays in your sources. InterLock stores configuration about them,
the structure it has catalogued, discovery summaries, and the audit trail.

## How changes reach the gateways

When an admin saves a source, role or policy, the admin writes it to
PostgreSQL and sends a notification. Every gateway listens and reloads the
affected configuration, so a change applies to the next request without a
restart. Cache invalidations travel the same way over Redis, so a write through
one gateway clears the cached answers held by every other.

## Deployment shapes

- **Local**: Docker Compose runs everything on one machine; the
  [quick start](/get-started/quick-start/) uses it.
- **Kubernetes**: a Helm chart runs the gateway as a StatefulSet (its audit
  spool needs a volume), the admin and workers as Deployments, with PostgreSQL
  and Redis provided by you. See [Deploy with Helm](/operations/deploy-with-helm/).

## Where to go next

- [The request pipeline](/concepts/request-pipeline/): what happens to one
  request, step by step.
- [Security model](/concepts/security-model/): what InterLock protects, and
  what it cannot.
