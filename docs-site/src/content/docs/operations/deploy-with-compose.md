---
title: Deploy with Compose
description: Run InterLock with Docker Compose on one machine.
sidebar:
  order: 2
---

`docker-compose.yml` runs the whole system on one machine: gateway, admin, two
workers, PostgreSQL and Redis, plus a one-shot migration job that runs before
the services start. It is built for local use: every port binds to
`127.0.0.1`, it uses development credentials, and it runs in development mode,
so the production checks do not apply.

```bash
docker compose up -d --wait
```

To use it beyond one laptop, override at least:

- the PostgreSQL and admin credentials (`INTERLOCK_DATABASE__PASSWORD`,
  `INTERLOCK_ADMIN__SECRET_KEY`);
- `INTERLOCK_AUTH__API_KEY_PEPPER`;
- `INTERLOCK_ENVIRONMENT=production`, and then everything the
  [production checklist](/operations/production-checklist/) enforces;
- the port bindings, deliberately.

Put local overrides in `docker-compose.override.yml` (see
`docker-compose.override.yml.example`); Compose merges it automatically and it
is ignored by git. For anything shared by a team, prefer
[Kubernetes with Helm](/operations/deploy-with-helm/).

To run this stack on a DigitalOcean Droplet for an evaluation, see
[Deploy on a DigitalOcean Droplet](/operations/deploy-on-a-digitalocean-droplet/).
