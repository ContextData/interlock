# InterLock E2E Governance Runbook

## Purpose

This seeded runbook is used by the compose-backed E2E test environment to
exercise InterLock discovery, ingestion, policy filtering, entity extraction,
and related-document flows without depending on external services. It describes
the expected behavior for PostgreSQL routing, HTTP proxy governance, approval
queueing, cache invalidation, redaction, and audit correlation in the local test
stack.

## Audience

The primary audience is InterLock platform engineering. Secondary audiences are
security reviewers, QA engineers, and release owners who need to understand why
the seeded environment exists and what production behaviors it is meant to
represent.

## Environment

The E2E stack runs with deterministic credentials and local-only dependencies:

- Control database: PostgreSQL used by Gateway, Admin, and Worker.
- Source database: `e2e_pg`, a separate upstream PostgreSQL database.
- HTTP source: `e2e_http`, a local mock upstream with JSON, text, CSV, mutation,
  failure, health, and call-log endpoints.
- Redis: shared cache and coordination service for the compose stack.
- Gateway: protocol entry point for PostgreSQL, HTTP, and MCP traffic.
- Admin: server-rendered HTMX console for approvals, sources, policies, and
  operational views.
- Workers: two worker processes used to verify independent service topology and
  future ingestion concurrency tests.

The source database is intentionally separate from the control database. Any
test that queries customers or orders through InterLock must prove that the
request reached `source-postgres` and did not accidentally execute against the
control database.

## Seeded Identities

The standard E2E agent represents a trusted automation identity:

- API key: `ag-e2e-api-key`
- PostgreSQL username: `e2e_pg_user`
- Roles: `analyst`, `writer`
- Team: `e2e`

The denied identity represents a blocked automation identity:

- API key: `ag-e2e-denied-key`
- Role: `blocked`

Both identities must resolve through the same identity contract before policy,
rate limit, cache, approval, redaction, and audit decisions are made.

## Governance Expectations

Every accepted, denied, cached, queued, rejected, expired, failed, or executed
request should emit a canonical audit event. At minimum, audit records should
include source id, protocol, identity id when authenticated, operation, status,
policy outcome, cache decision, redaction details, latency, and correlation
metadata.

The shared pipeline should be the only governance path for PostgreSQL, HTTP,
and MCP traffic. Protocol adapters may normalize protocol-specific request
details, but they should not bypass authentication, policy, write safety,
redaction, cache handling, approval logic, or audit emission.

## PostgreSQL Source Checks

PostgreSQL E2E tests use the `customers`, `orders`, and `mutation_log` tables in
the source database.

Expected read behavior:

- The PostgreSQL client authenticates to InterLock before any upstream
  connection is opened.
- The resolved identity is attached to the session context.
- Simple query reads are normalized, governed, optionally cached, redacted, and
  audited.
- Extended-protocol reads using prepared statements are governed before
  execution and audited after response processing.
- PII fields such as `email` and `ssn` are redacted according to the seeded
  policy metadata.

Expected write behavior:

- Risky writes from authenticated identities queue for approval.
- Writes with unresolved identity or unresolved source are rejected instead of
  queued.
- Approved writes execute exactly once through the approval queue.
- Successful writes invalidate deterministic cache entries for affected source
  data.

## HTTP Source Checks

HTTP E2E tests route through `e2e_http` to the local mock upstream.

Expected read behavior:

- `GET /json/customer` returns a JSON customer document suitable for redaction
  and cache assertions.
- `GET /text/customer` returns text content that can be scanned for PII-like
  values.
- `GET /csv/customers` returns tabular content for future CSV redaction tests.
- InterLock authentication headers are stripped before the upstream receives
  the request.
- Successful reads write audit events with protocol `http` and source
  `e2e_http`.

Expected write behavior:

- `POST`, `PUT`, `PATCH`, and `DELETE` requests are classified as writes unless
  endpoint policy explicitly says otherwise.
- Destructive requests queue for approval before any upstream mutation occurs.
- Rejection and expiry do not call the upstream mutation endpoint.
- Approval execution must be idempotent and observable in both the approval
  record and the audit log.

## Cache and Invalidation

The E2E stack starts with deterministic-first cache behavior and semantic
verification disabled. This keeps the certification suite reproducible and
independent of model downloads.

Cache expectations:

- Cache strategy is selected from the registered source configuration.
- Deterministic hits should avoid duplicate upstream calls for eligible reads.
- Redacted response bodies are safe to cache for the requesting identity scope.
- Approved or auto-approved writes invalidate stale deterministic cache entries.
- Audit records should distinguish cache hit, cache miss, cache tier, and stale
  fallback when that mode is enabled in future tests.

## Discovery Expectations

This runbook is seeded into discovery as an engineering runbook document with
topics `interlock`, `e2e`, and `runbook`. It should be searchable by title,
summary, topic, category path, entity, and full-text terms such as approval,
audit, PostgreSQL, HTTP, redaction, cache, and source routing.

Expected discovery metadata:

- Title: InterLock E2E Governance Runbook
- Document type: markdown
- Category path: `engineering.runbooks`
- Audience: engineering
- Primary entity: InterLock
- Related concepts: PostgreSQL proxy, HTTP proxy, approval queue, audit log,
  cache invalidation, source routing, redaction

Discovery and ingestion tests may use this document to verify category filters,
entity filters, time-range filters, related-document expansion, policy
filtering, and reciprocal-rank fusion once those E2E tests are added.

## Failure Signals

Treat the following as E2E failures:

- A PostgreSQL query succeeds against the control database instead of
  `source-postgres`.
- An HTTP mutation reaches the upstream before approval.
- An authenticated request writes an audit event without an identity id.
- A denied request succeeds or fails without an audit trail.
- Redaction changes a response body but leaves an invalid content length.
- Running the seed script twice changes test semantics or creates duplicate
  policy, source, identity, asset, or ingestion records.

## Operational Notes

The complete local validation flow is:

```bash
make e2e-up
make e2e-seed
make test-e2e
make e2e-down
```

The one-command clean certification flow is:

```bash
make e2e
```

The full `make e2e` path should build the services, apply migrations, seed
control and source data, run the E2E suite, print logs on failure, and tear down
volumes after completion.
