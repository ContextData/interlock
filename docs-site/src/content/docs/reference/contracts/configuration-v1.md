---
title: "Configuration V1 Contract"
description: "The configuration surface InterLock promises to keep stable in V1."
normative: true
sidebar:
  order: 3
---

Status: stable release-candidate surface for `1.0.0-rc.1`.

## Sources And Precedence

InterLock loads configuration in this order, with later sources winning:

1. Application defaults.
2. YAML from `INTERLOCK_CONFIG_PATH`, or the first local `config.yaml` /
   `config.yml` when no explicit path is set.
3. Environment variables using the `INTERLOCK_` prefix and `__` for nesting.

Example:

```text
INTERLOCK_ENVIRONMENT=production
INTERLOCK_SERVICE_ROLE=gateway
INTERLOCK_GATEWAY__HTTP_PORT=3000
INTERLOCK_DATABASE__SSL_MODE=verify-full
INTERLOCK_AUDIT__DURABILITY_MODE=strict
```

`INTERLOCK_` is a V1 compatibility prefix retained from the internal Python
namespace. Public product branding is InterLock. A future replacement prefix
must follow the deprecation policy and accept `INTERLOCK_` through at least the next
minor release.

Unknown top-level and nested keys are rejected. YAML syntax/read errors and
invalid ranges fail startup rather than falling back silently.

## Top-Level Sections

| Section | Contract |
| --- | --- |
| `environment` | `development`, `test`, or `production` |
| `service_role` | `all`, `gateway`, `admin`, `worker`, or `migration` |
| `gateway` | Listener addresses, ports, protocol limits, TLS, and timeouts |
| `admin` | Admin listener, session/CSRF secret, cookie policy, bootstrap behavior. `admin.bootstrap_password` sets the first admin's password; when unset the first admin is `admin` / `admin` and must change it at first sign-in |
| `worker` | Worker identity, concurrency, heartbeat, and lease duration |
| `database` | InterLock control PostgreSQL connection and verified TLS |
| `redis` | Redis URL and pool bound |
| `auth` | API-key session, pepper, legacy-key migration, and custom-key policy |
| `cache` | L1/L2 TTLs, consistency barrier, generation namespace, pub/sub reconnect |
| `pii` | Fast/deep scanner enablement and free-text field patterns |
| `audit` | Buffer, retry, strictness, durable spool, and partition maintenance |
| `ingestion` | Worker ingestion enablement, retry, heartbeat, and timeout |
| `notifications` | Slack delivery for write approvals; credentials MUST be secret references |
| `approvals` | Write-approval queue behaviour, including `approvals.expiry_seconds` |
| `catalog` | Source structure capture: scan on save, scheduled refresh, and per-scan caps |
| `semantic_cache` | Reserved tuning; serving is disabled in the V1 product boundary |
| `observability` | OTel endpoint/instrumentation and readiness/stat exposure |

The Pydantic schema in `src/interlock/config.py` is the field-level source of truth
for accepted names and ranges. This contract stabilizes section names and
semantics, not every default value.

## Production Invariants

When `environment=production`, startup fails unless all applicable invariants
hold:

- Admin signing secret is at least 32 characters for Admin roles.
- Admin cookies are secure.
- API-key HMAC pepper is at least 32 characters.
- Legacy unpeppered API-key hashes are disabled.
- The PostgreSQL listener has TLS certificate/key and requires client TLS, or
  explicitly declares trusted TLS offload.
- `database.ssl_mode` is `verify-full` and `database.ssl_ca_file` identifies a
  CA file for the control PostgreSQL database.
- `audit.durability_mode` is `strict`.

Production operators SHOULD also use an immutable image digest, external
secrets, a persistent audit spool, and dependency-aware readiness.

## OpenID Connect

`auth.oidc` is a V1 release-candidate interface for Admin authorization-code
SSO and agent bearer JWTs. OIDC identifies a pre-provisioned principal; local
Admin roles and agent source-role grants remain authoritative.

| Field | Contract |
| --- | --- |
| `enabled` | Enables discovery and OIDC verification for the applicable service role. |
| `issuer_url` | Exact issuer expected in discovery and token `iss`; production requires HTTPS. Trailing-slash variants are not equivalent. |
| `admin_client_id` | Audience/client identifier for Admin authorization-code login. |
| `admin_client_secret` | Confidential client secret, supplied through a secret store. |
| `admin_redirect_uri` | Registered callback URI; production requires HTTPS. |
| `agent_audience` | Expected audience for agent bearer JWTs. |
| `scopes` | Admin authorization request scopes; must include `openid`. |
| `flow_ttl_seconds` | TTL for one-time Redis-backed state, nonce, and PKCE verifier. |
| `local_break_glass_enabled` | Keeps password login available to local `owner`/`admin` identities only. |
| `admin_group_role_map` | Map from exact IdP group names to allowlisted InterLock Admin roles. Local database roles are unioned with mapped roles at login. |
| `allow_insecure_endpoints` | Explicit local-development exception for HTTP IdP endpoints. Production requires `false`. |

Admin login uses authorization code plus PKCE S256, nonce, state, and atomic
one-time flow consumption. Discovery `issuer` must exactly equal
`issuer_url`; authorization, token, user-info, and JWKS endpoints must use
HTTPS unless the explicit development exception is enabled. A token with
multiple audiences requires `azp` equal to the configured client/audience.

OIDC subjects are never auto-provisioned. Security administrators manage
exact subject mappings through these authenticated, CSRF-protected APIs:

- `PUT` / `DELETE /api/admin-auth/oidc/admins/{admin_id}`
- `PUT` / `DELETE /api/admin-auth/oidc/agents/{identity_id}`

Changing an Admin username, role set, enabled state, or OIDC subject advances
its authorization version. The middleware compares that database version on
every request and revokes stale sessions immediately.

## Secrets

Literal secrets must be supplied by an external secret mechanism, environment
injection, or connector-supported secret reference. Public YAML examples must
not contain usable credentials. Admin rendering and audit output expose only a
credential mode, masked value, or safe reference.

Changing the Admin signing secret invalidates Admin sessions. Changing the API
key pepper invalidates HMAC-key authentication unless a controlled dual-pepper
migration is provided by a future release.

## Compatibility

- Adding an optional field is backward-compatible.
- Renaming/removing a field follows the one-minor-release policy.
- Tightening a value range may occur in a patch release only for a security or
  data-integrity reason and must be called out in release notes.
- Production startup checks may become stricter in a minor release; release
  notes must include required values and rollout order.
- Source connector configuration is connector-owned and may add optional
  fields. Removal or semantic reuse of an existing stable field is breaking.

SAML and alert-rule delivery settings are not part of the current V1 runtime
schema. Documentation must not instruct operators to set unrecognized keys.
When implemented, they enter through an additive release-candidate contract.

## Notifications

Approval notifications are optional and disabled by default. Enabling them
requires at least one Slack target.

| Field | Meaning |
| --- | --- |
| `notifications.enabled` | Master switch; when false no notifier is constructed |
| `notifications.slack_bot_token_ref` | Secret reference to a bot token; requires `slack_channel`. Preferred over a webhook |
| `notifications.slack_webhook_url_ref` | Secret reference to an incoming-webhook URL |
| `notifications.slack_channel` | Channel for the bot transport |
| `notifications.admin_base_url` | Base URL used to link a message to the approval page |
| `notifications.approval_events` | Which of pending, approved, rejected, expired, failed to send |
| `notifications.timeout_seconds`, `notifications.max_attempts` | Per-attempt timeout and bounded retries |
| `notifications.allow_private_egress` | Permits a non-public delivery target; default false |

The `*_ref` fields MUST be secret references. A literal value is rejected at
startup, as is a reference that cannot be resolved or resolves to an empty
string: a notifier that silently never delivers is worse than a boot failure.
`notifications.slack_webhook_url` accepts a literal URL and is deprecated.

Delivery is best-effort by design. A failure is logged and dropped, never
raised into the request that triggered it, because the governance decision has
already been made and recorded by then.

`catalog` controls the source catalog: each source's structure - for SQL sources
its schemas, tables, views and columns - captured by the worker after every
save and refreshed every `catalog.refresh_interval_seconds` (default `86400`,
minimum `900`). Scans connect with the source's own configuration, so secret
references and the production TLS requirement apply. They read structure only,
never row data. Every scan is bounded by `catalog.max_nodes`,
`catalog.max_schemas`, `catalog.max_tables` and `catalog.max_columns_per_table`;
a scan that hits a cap records `truncated` and never reports what it did not
reach as removed. `catalog.enabled: false` stops all scanning.

`approvals.expiry_seconds` (default `900`) bounds how long a pending approval
may be acted on. Operators who rely on notifications SHOULD raise it, since the
default predates any way of being told an approval exists.
