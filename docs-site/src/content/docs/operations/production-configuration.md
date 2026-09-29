---
title: Production configuration
description: The settings that matter most in production, and how to supply them safely.
sidebar:
  order: 6
---

Every setting is listed in [Configuration](/reference/configuration/). Those
that matter most in production:

| Setting | Why |
|---|---|
| `environment: production` | turns on the start-up checks in the [production checklist](/operations/production-checklist/) |
| `admin.secret_key`, `auth.api_key_pepper` | from Secrets; 32+ characters each |
| `database.ssl_mode`, `database.ssl_ca_file` | `verify-full` against the control database's CA |
| `audit.durability_mode`, `audit.spool_path` | `strict`, with the spool on a persistent volume |
| `gateway.pg_tls_cert_file`, `gateway.pg_tls_key_file` | TLS on the PostgreSQL listener |
| `gateway.mcp_allowed_origins` | browser origins allowed to call `/mcp`; leave empty unless a browser client needs it |
| `cache.source_generation_prefix` | must be identical on every gateway and admin |
| `approvals.expiry_seconds` | how long a queued write waits for a reviewer |
| `catalog.refresh_interval_seconds` | how often sources are rescanned |
| `notifications.*` | Slack approval notifications |
| `observability.otlp_endpoint` | where traces and metrics go |

Supply secrets as environment variables from your secret store, never in a
committed YAML file. Unknown keys are rejected at start, so a typo fails loudly
instead of being ignored.
