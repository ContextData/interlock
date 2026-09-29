# Changelog

InterLock was developed in a closed repository before its first public
release. This repository was forked from the closed development repository;
earlier history, pull requests and reviews remain private. The release
candidates below were built and published from there, and this file is their
record. Entries are grouped by the release that first shipped them.

## Unreleased

### Added

- `make smoke-eval` runs the quick start as an acceptance test: from an empty
  Compose project it signs in with the default password and changes it,
  registers the sample database through the wizard, grants an agent the `read`
  role, and checks the redacted rows over both protocols, a refused `DELETE`
  and the audit record. CI runs it beside the end-to-end suite. The evaluation
  guide now has two tracks: start from empty, or explore the seeded demo.

### Fixed

- The same SQL from the same agent over MCP and then over the PostgreSQL wire
  protocol shared one cache entry, so the second read got the first's format:
  `psql` received MCP's JSON and failed with `lost synchronization with
  server`, and an MCP agent could receive PostgreSQL wire bytes as its result.
  The cache key now includes the protocol (key format v6, so existing entries
  become misses), and each protocol checks a cached entry's format before
  serving it. Reported by an independent evaluation of rc.13; covered by
  end-to-end tests that compare the rows `psql` and MCP decode, in both orders.
- A redacted answer served from cache was audited with `pii_detected` false,
  because no scanner ran on the hit. The redaction applied when the answer was
  cached now travels with the entry in both cache tiers, and PostgreSQL, MCP
  and HTTP cache hits record it; the event page marks it as redaction from
  when the answer was cached.
- The audit event page showed an empty correlation ID, which lives in the
  request metadata, and printed policy decisions and metadata as Python
  dictionaries; both now render properly. The CSV export gains `protocol` and
  `correlation_id` columns (appended, so existing columns keep their
  positions), and **Audit & Costs** gains the protocol filter the docs
  described.
- An MCP query the database rejected, such as one naming a column that does
  not exist, returned only `tool execution failed`. Query errors (SQLSTATE
  classes 42, 22, 23 and 0A, and the MySQL equivalents) now return their code
  and the database's primary message, as a readable `isError` result for
  handshake clients and `-32602` with the text for modern ones. Connection and
  server errors stay opaque. Both carry a correlation ID that matches the
  audit event.
- The console could not register a source on a private network, such as the
  quick start's sample database, because only the API could set
  `allow_private_egress`. The new-source form, the wizard and the edit page now
  have an **Allow a private network address** option, with the same rules as
  the API.
- The quick start stays in the console: the sample database is registered
  through the wizard with the private-network option, the wizard's default
  `read` role is the agent's role (the old step that asked for one hand-made
  statement did not match the role editor), both protocols run the same
  statement, `psql` runs inside the stack, the refused write is shown over MCP,
  and a port override is given. Compose no longer warns about the optional
  Anthropic and Mistral keys; the guide says no model key is needed.
- HTTP sources can set `probe_path`, the route **Test Connection** requests,
  for APIs whose root answers 404; a failed test now names the path it
  requested. It must be a path on `base_url`.
- Connector pages and the console call a connector's status its
  *implementation*, separate from release status, and a semantic cache
  strategy says semantic serving is disabled. The MCP tools reference names
  the source-role action each tool needs.
- The console's browser tab now follows sidebar navigation instead of staying
  on the first page's title, and no longer prints that title in the page.
- Docs: readable sidebar labels for guide groups; the testing page says
  `make load` needs Docker and deletes its project's volumes; contributor
  notes moved out of known limitations, and its release-gate line is current.

### Documentation

- Two DigitalOcean deployment guides: an evaluation on one Droplet with Docker
  Compose, reached through an SSH tunnel, and a production deployment on DOKS
  with Managed PostgreSQL and Valkey, Traefik and Let's Encrypt. Each was
  followed from an empty account to a governed, redacted query and torn down.
  The DOKS guide sizes PostgreSQL by connections: six pods with the default
  pool exhaust the 1 GB plan, so it uses the 2 GB plan and a pool of 4.
- The documentation site is published at https://interlock.contextdata.dev by
  DigitalOcean App Platform, from the spec in `.do/docs-app.yaml`, and is rebuilt
  from `main` on every push. Canonical URLs and the sitemap use that address;
  `DOCS_SITE_URL` builds for another.
- The DigitalOcean (DOKS) deployment and live certification gate is closed:
  `v1.0.0-rc.13` was deployed from its signed digests to a disposable cluster,
  certified (35/35 governed-core end-to-end tests, readiness across an upgrade)
  and torn down by the automated workflow. The release process page cites the
  run, the date and both digests.

## 1.0.0-rc.13 - 2026-09-28

### Security

- **A disabled source kept serving cached answers.** Disabling or deleting a
  source removed it from the gateway's registry, but the MCP query handler
  consulted the cache before the registry, so any statement cached while the
  source was enabled was still answered afterwards; only new statements were
  refused. The handler now refuses an unknown source before the cache, an open
  PostgreSQL-wire session ends with `3D000` on its next message, and a registry
  reload clears the cache and connection pools of every source it stops
  loading. The admin console also clears a source's cache when it is edited,
  disabled or deleted, using the gateway's generation prefix: it had been
  advancing a counter under a default prefix no gateway read.

- **The HTTP proxy ignored a source's cache strategy.** It always used the
  gateway-wide strategy, so a source set to `bypass` was still cached over
  HTTP. It now resolves the source's own strategy, and a miss no longer falls
  through to the shared L2 cache.

### Added

- **A fresh install can be signed in to.** With no admin yet, the console
  creates `admin` with the default password `admin`. That account reaches only
  the new change-password page until it sets a password of at least 12
  characters that is neither the default, its username nor the current one;
  HTML is redirected there, htmx gets `HX-Redirect`, and API calls answer 403
  `password_change_required`. `INTERLOCK_ADMIN__BOOTSTRAP_PASSWORD` still
  chooses the first password, and is not forced to change. Previously the
  quick start produced no login at all unless that variable was set.

- **Admins can change their own password** at `/auth/change-password`, linked
  from the sidebar. The change is throttled, audited as `password.change`
  (fail-closed), and revokes every other session of that admin: migration 019
  makes a password change bump `authorization_version`.

### Fixed

- The quick start works as written: `docker compose up` needs no
  `config.yaml`, the admin console keeps a stable signing key across restarts,
  and every published port binds to `127.0.0.1`. The unused `8080` mapping is
  gone.

- The Helm chart's default image is the chart's own `appVersion` instead of a
  hardcoded `1.0.0-rc.1`, and the chart names its home, sources and
  maintainers.

- The Salesforce and Google Workspace client libraries (Apache-2.0) are in the
  published image, so those connectors no longer fail on a missing import.

- Install hints name the real package (`interlock-runtime[pii]`,
  `interlock-runtime[otel]`), OpenTelemetry reports the running version, and
  CI measures coverage of `interlock` rather than the pre-rename namespace.

### Documentation

- **The documentation is a developer guide.** It is now a Starlight site in
  `docs-site/`, organised as Get started, Concepts, Guides, Reference,
  Operations and Project. The quick start runs a sample database with
  `docker compose --profile quickstart up` and reaches a governed, redacted,
  audited query in a few steps, all verified against a fresh install. The
  reference for configuration, the admin API and its roles, MCP tools,
  connectors, role conditions, feature status and make targets is generated
  from the code and checked for staleness in CI. Contracts and runbooks moved
  into the site unchanged in substance and remain normative; the old `docs/`
  paths are mapped in `docs/README.md`. The README is a short introduction.
- Corrected while rewriting: a Vault reference is `vault://path/key` (the
  setup guide showed a `#key` form the resolver never read); the policy
  `classifications` condition is documented as matching no agent traffic today;
  adding admin accounts beyond the first needs SQL, now documented.

- Corrected claims the code does not back: policy `redact_columns` is applied
  to MCP and HTTP responses but not on the PostgreSQL wire, where only the PII
  scanner redacts; S3 and Spaces have no reachable governed write in V1; GitHub
  is active by default but its governance is not exercised by any test stack.
  The README's ports, admin roles, `psql` usage, extras and layout match the
  code.
- `NOTICE` describes the published image as it is: permissive licenses plus
  LGPL-3.0 (PyGithub, python-gitlab) and MPL-2.0 (certifi, tqdm, orjson), and
  the vendored htmx (BSD-2-Clause) and fonts (OFL). `make license-report`
  installs exactly what the image installs, writes a per-package inventory,
  and fails on any license outside that position.
- This changelog is split into one section per release candidate.
- `SECURITY.md` names a private contact, security@contextdata.ai, alongside
  GitHub private vulnerability reporting, with response times and supported
  versions. Issue creation links to it.

## 1.0.0-rc.12 - 2026-09-25

### Changed

- The Connectors page no longer has an Add Data Source button. Sources are
  added from the Data Sources page; the Connectors page only manages which
  connectors are active.

## 1.0.0-rc.11 - 2026-09-25

### Security

- **Editing a source role could replace another source's role statements.**
  The role update route deleted and rewrote statements by role id alone, so a
  console form posted under one source with a role id belonging to another
  source rewrote that other role. The update and edit routes now return 404
  unless the role belongs to the source in the URL, the statement delete is
  scoped through the role's source, and the rewrite runs in one transaction.

### Added

- **Connector activation.** New sources can only be registered on an active
  connector. A deployment starts with PostgreSQL, MySQL, Amazon S3, Slack,
  GitHub and HTTP/REST active (migration `018`, which also activates any
  connector an existing source already uses). The Connectors page is now a
  table of active connectors, with the rest under "Available to activate", and
  source admins can activate or deactivate them there, audited. Deactivation
  only hides a connector for new sources; existing sources keep working. The
  wizard, the console form, `POST /api/data-sources` and both Test Connection
  routes refuse an inactive or unknown connector (an unknown key used to fall
  back to HTTP/REST). The connector dropdowns list active connectors only,
  grouped by category instead of "Existing / Tier 1 / Tier 2", and `GET
  /api/connectors` returns the list.

- **Delete a source role from the console.** The roles table on a source's page
  has a Delete action. It is refused, naming the identities, while any of them
  holds an active grant of the role, because the delete would otherwise revoke
  those grants silently. Otherwise the role is removed and the admin audit
  entry `source_role.delete` keeps the role, its statements and its revoked
  grant history. Policies whose `roles` condition names the role are listed in
  the confirmation.

### Changed

- **The role editor offers each source's own options.** Action and Resource type
  are dropdowns of what the source's connector supports, grouped as Read,
  Write, Admin and schema, and Discovery; choosing an action narrows the
  resource types to the ones it is checked against. Resource pattern shows how
  to write a pattern for the chosen type and suggests catalogued names
  (`sales.customers.*`, `s3://bucket/prefix/*`, a Slack channel's messages).
  Picking from the catalog now works for non-SQL sources: an S3 prefix, Slack
  channel, GitHub repository, index or collection is written as the pattern
  its requests actually carry. Conditions are typed controls for the chosen
  action - risk-level and HTTP-method checkboxes, a Require WHERE checkbox,
  lists with catalogued suggestions for channels, repositories, indices and
  collections - with Edit as JSON kept for anything else, whose keys the
  controls never overwrite. A statement saved before these options existed
  is shown as "Kept as saved" and posts back unchanged.

- **Source role statements must fit the source.** Saving a role now refuses a
  statement whose action the source's requests never carry (a SQL action on an
  S3 source), a resource type the action is never checked against, a
  condition key the evaluator does not read (`columns`, for example, which an
  allow used to ignore and so grant every column), a condition value of the
  wrong type, and a deny whose condition can never be set on that source's
  requests, since that deny would never apply. A statement already stored on
  the role is kept as saved until it is edited, so every existing role still
  saves. The role editor's warnings show the same problems while typing. A new
  role starts from the connector's own reader template: a Slack role used to
  start with HTTP/REST's `http.get`.

- **Audit & Costs shows usage by outcome instead of cost units.** The "cost
  unit" was a weight per audit row (1.0 per request, 0.1 per cache hit,
  including denied and failed requests), shown beside the request count as
  "33.2 (44)", which read like money. InterLock does not price requests, so
  the section is now "Usage breakdown": requests by identity, source and team
  as stacked bars of served by the source, served from cache, denied, queued
  and errors, with a legend, and summary cards for the same period. A period
  selector (24 hours, 7 days by default, 30 days) applies to the cards and the
  bars alike; before, they always covered all history. Identities are named
  (deleted ones included), and requests with no team are counted under "No
  team" rather than dropped, so team totals add up. An audit event shows the
  cost a source reported itself, such as Snowflake credits, as fields.

- **Source ids are generated from the display name.** Neither the wizard nor
  the console form asks for a source id any more: `Sales PostgreSQL` becomes
  `sales_postgresql`, suffixed `_2`, `_3` on collision, shown on the review step
  and, with a Copy button, on the source's page, whose title is now the display
  name. `source_id` is optional on `POST /api/data-sources` and is validated on
  the server when supplied (only the HTML input checked its form before). The
  data sources table drops the Source ID and Connection columns, links the
  name, and is rendered by one template, so the table returned after a save no
  longer differs from the page's.

### Fixed

- **Qdrant's default `collection_reader` role never allowed its own queries.**
  The Qdrant connector computed a collection resource type but never set it on
  the request, so the evaluator compared the template's `search.collection`
  statement with `search.index` and refused. The request now carries it.
  Relatedly, `introspect()` no longer advertises constraint keys the evaluator
  never reads (Google Workspace's `drive_ids`, `mime_types` and the rest,
  GitLab's `groups` and `pipelines`): a statement relying on them was silently
  unconstrained.

- **A source's Governance panel listed the wrong policies and said little.**
  It matched only the singular `source_id` condition, so a rule scoped to
  another source through `source_ids` appeared on every source; it listed
  disabled rules; and it showed a rule with no effect as `allow` where the
  engine denies. It is now "Policies applied to this source": the enabled
  rules the engine would consider, chosen by the engine's own source test, in
  evaluation order, each with a plain-English sentence of what it does, its
  effect, and how many requests to this source it decided in the last 7 days,
  under a line explaining first-match-wins and default deny. The Policies list
  uses the same sentences, so no rule reads as "Custom conditions" any more,
  and a condition key the engine ignores is named as ignored.

- **Audit views showed `#7` instead of a name for deleted identities.**
  `audit_log` keeps only the identity id and deleting an identity removed its
  row, so the overview, audit table, event detail, write safety, source detail,
  policy analytics and alert pages lost the name. Deleting an identity through
  the admin API now leaves a tombstone (migration `017`), backfilled from the
  admin audit log's earlier `identity.delete` entries. Every view shows
  `name (#7)`, `name (#7, deleted)`, `Deleted identity #7` when no name was
  ever recorded, or `No identity` for a request refused before sign-in. The
  audit CSV export gains a trailing `identity_name` column.

## 1.0.0-rc.10 - 2026-09-25

### Fixed

- **`interlock_describe_access` missed tables reached through a legacy
  spelling.** On MySQL and Snowflake a role written as `public.<table>` still
  reads the table through the legacy alias, but the `resources` view checked
  only the real name and told such an agent it reached nothing. Found on the
  live deployment after rc.9; the view now checks the same spellings as
  enforcement.

## 1.0.0-rc.9 - 2026-09-25

### Security

- **Multi-statement SQL on the PostgreSQL wire could read a denied table.**
  With a deny policy or source-role deny on `orders`, `SELECT * FROM orders`
  was refused, but `SELECT 1; SELECT * FROM orders` over the PostgreSQL wire was
  allowed and the upstream returned rows from the denied table. The proxy kept
  only the first statement's metadata and passed governance no tables at all, so
  the evaluator saw resource `*.*`, which no table-specific deny matches, and a
  policy's `tables` condition had nothing to intersect. Every release up to and
  including `1.0.0-rc.8` is affected.

  Governance now sees every table any statement in the message reads. The MCP
  path kept only the first statement's tables too; it was contained because the
  driver refuses several commands in one call, and it now takes the union as
  well so containment no longer depends on the driver. Writes in a
  multi-statement message were already refused and still are.

- **Functions that change session state or read data unseen are no longer
  treated as reads.** `SELECT set_config('search_path', ...)` was accepted though
  a plain `SET` was refused, and on a persistent PostgreSQL-wire session it
  changes how every later bare table name resolves. `table_to_xml`,
  `query_to_xml`, `schema_to_xml`, `database_to_xml`, `dblink`, `pg_read_file`,
  `pg_ls_dir`, `pg_stat_file` and `lo_get` read table or server data where no
  table appears in the statement - `table_to_xml('public.orders', ...)` returned
  a table a deny policy refused to `SELECT`. These, plus `pg_terminate_backend`,
  `pg_cancel_backend`, `lo_import` and `lo_export`, are now classified high risk:
  refused on the PostgreSQL wire, routed to approval on MCP. Queries that relied
  on them as plain reads will need approval.

### Added

- **Drift shows who can already read what appeared, and the audit trail is
  read against the catalog.** When a scan finds a new SQL table or column, the
  change row records which enabled roles on the source would already allow
  reading it without denying it, and how many identities hold one of them; the
  Catalog section's drift list shows it. A new **Access analytics** page, per
  source and over 7, 30 or 90 days, lists the most-read tables, PII and
  sensitive columns agents actually read, refusals by table, and catalogued
  tables no governed request touched.

- **The catalog covers sources that are not SQL.** Scans now record S3 and
  Spaces prefix trees (four levels, 5,000 prefixes), Slack channels, GitHub
  repositories, Salesforce's configured objects and fields, OpenSearch and
  Elasticsearch indices with their mapped fields, and Qdrant collections with
  their payload fields - structure only, never content. Each collector connects
  through its adapter's own code, so configuration checks, egress rules and
  secret references apply, and reports truncation rather than claiming
  completeness. These catalogs are inventory: the console, the role editor and
  `interlock_describe_access` all label them as not enforced on agent paths.

- **Agents are told which tables and columns they can reach.**
  `interlock_describe_access` adds a `resources` entry per source: for SQL
  sources, the catalogued tables and columns the caller's roles allow and do not
  deny - never naming anything it cannot read - with a marker on any table a
  policy would refuse, the catalog's scan time, and caps; non-SQL sources say
  their resources are not enforced yet. `interlock_describe_source` on a
  catalogued PostgreSQL source now returns the catalogued columns of every
  schema, limited to tables the caller may describe, where a caller whose roles
  named individual tables used to be refused outright.

- **Role and policy editors read the catalog.** Each role statement and each
  policy's tables, columns and redact-columns fields can be filled from a
  catalog picker: focus a field, open the source's tree, choose **Use** on a
  schema, table or column. Warnings update as the form changes: a pattern that
  matches nothing (with the age of the last scan), a legacy `public.` spelling,
  what a column-level allow costs (`SELECT *`, `COUNT(*)` and DELETE need the
  whole table), a source that has not been scanned, and patterns on sources
  whose agent paths do not check them yet. Policies gain a `columns` condition
  field, and warnings for unknown or ambiguous table and column names.
  `POST /api/policies/validate` returns the same warnings for a rule without
  saving it. Warnings never block a save.

- **InterLock records what each source contains.** When a source is saved -
  through the API, the console form, the edit page, the wizard, or by
  re-enabling it - the worker scans its structure and stores it in a new source
  catalog: for PostgreSQL, MySQL, MariaDB and Snowflake, every schema, table,
  view and column the source's login can see, with types, nullability, row
  estimates and a heuristic classification. `POST
  /api/data-sources/{id}/catalog/scans` queues a scan on demand and `GET` on the
  same path lists the scan history.

  Scans connect with the source's own configuration, through the same code
  agent queries use, so secret references and the production TLS requirement
  apply. The PostgreSQL scanner this replaces opened its own pool with a plain
  password and no TLS settings, which is why it could not scan a source
  configured the way production requires; nothing ever read what it wrote.

  Scans read structure only, never row data, and every scan is bounded by caps
  (`catalog.max_tables`, `catalog.max_columns_per_table` and others). A capped
  scan records `truncated` and never reports what it did not reach as removed.
  A source's first scan is a baseline; later scans record what was added,
  removed or changed. A failed scan stores a scrubbed reason - a refused private
  host reads `egress_blocked`, not `internal` - and never a connection string.

  The worker now holds a source registry and connection manager, and the live
  compose overlay passes source references to the workers, since scans connect
  to sources. Roles, policies, agents and the console start reading the catalog
  in the following changes.

- **The catalog stays current, and the console shows it.** Every worker runs a
  scheduler that refreshes each catalogued source once its last successful scan
  is older than `catalog.refresh_interval_seconds` (a day by default), retries a
  failed source after `catalog.failure_retry_seconds`, and trims scan history
  and old drift rows. A source can set `catalog_refresh_interval_seconds` in its
  metadata to change its own interval, or `0` to opt out - useful for a
  Snowflake warehouse whose scans cost credits. Two workers ticking together
  still queue one scan per source. Asking for a scan that is already waiting
  brings it forward and records who asked, so Rescan never waits behind the
  scheduler's jitter.

  A source's detail page gains a Catalog section: the last scan and any error,
  what was recorded, the scanning login's search path, a browsable tree,
  per-column classifications, drift since the previous scan with acknowledge,
  scan history, and Rescan. A new Catalog page searches every source's
  structure by name, type and classification. Classifications are stored apart
  from what scans write, so a rescan never erases an admin's decision.

  Discovery now indexes tables from the catalog after a scan changes a source,
  and removes tables the catalog records as gone; it no longer introspects
  sources separately. Rescan Sources on the Discovery page queues catalog scans
  for every source instead of scanning PostgreSQL sources from the admin
  process, and `admin.catalog_on_startup` now only queues a first scan for
  sources that have never had one. The old startup scanner is removed.

### Changed

- **Column-level rules are enforced on agent paths.** Role statements and
  deny policies naming columns were accepted and then did nothing: every SQL
  request was evaluated as the whole table, so a deny on `customers.ssn` never
  fired and an allow on `customers.name` refused every query on the table.
  Requests on MCP and the PostgreSQL wire now carry one resource per column
  they touch, resolved with the source catalog - SELECT list, WHERE, JOIN,
  GROUP BY and ORDER BY, the columns an UPDATE sets and an INSERT writes, and
  `SELECT *` expanded. Wildcard statements cover every column, so table-level
  roles behave as before.

  What tightens: a column-level deny now fires; `COUNT(*)` and DELETE need
  access to the whole table; and where the columns a query reads cannot be
  determined - a whole-row reference such as `to_jsonb(c)`, a column or table
  the catalog lacks - a request meeting a column-level deny on that table is
  refused with a reason naming it. A deny policy's `columns` condition now
  matches too. Policy `redact_columns` applies to MCP query results, following
  each output column to the columns it derives from, so `SELECT ssn AS tax_id`
  is redacted.

- **SQL table names are resolved the way the database resolves them.** Every
  unqualified table used to be governed as `public.<table>`, which is right
  only for a PostgreSQL source whose search path starts with `public`. On MySQL
  there is no `public` schema, so a role written against the real database
  name never matched; on a PostgreSQL login whose search path starts elsewhere,
  roles were evaluated against the wrong table. With a source catalog, a bare
  name now resolves to the first schema on the scanning login's search path
  that holds it (PostgreSQL), the configured database (MySQL, MariaDB) or the
  configured schema (Snowflake). Three-part names keep their database, and a
  Snowflake reference into another database is refused.

  Existing roles keep working where they used to: on MySQL and Snowflake,
  statements written as `public.<table>` still match through a legacy alias,
  for allows and denies alike. On PostgreSQL, schemas earlier on the search
  path than the one that holds a table are also checked against `deny`
  statements, so a table created there since the last scan cannot route around
  a deny. What tightens: on a PostgreSQL source whose search path does not
  start with `public`, an allow written as `public.*` no longer covers bare
  names, which fail closed. `GET /api/catalog/naming-report` lists every role
  statement and deny policy whose meaning changes, with the spelling to use.

- **A deny policy matches every spelling of its tables.** A policy's `tables`
  condition was compared with the query's text exactly, so a deny on `orders`
  did not stop `SELECT * FROM public.orders`. Deny rules now match bare,
  schema-qualified and legacy spellings. Allow rules still match only as
  written: rules are first-match by priority, and a broader allow could
  pre-empt a lower-priority deny.

### Fixed

- **The console's dry-runs explained decisions the gateway would not make.**
  In SQL mode the source-role dry-run handed the raw statement to a connector
  adapter that never parsed it, and reported `db.execute_raw` on `*` for a plain
  SELECT the gateway allows as `db.table.select`. Both dry-runs
  now derive the request through the gateway pipeline's own method - verb,
  every statement's tables, write classification and name resolution - and
  `tests/e2e/test_sql_governance_parity.py` compares the dry-run with the audit
  row of the same statement sent through the gateway.

- **The wizard's PII flags were thrown away.** Ticked columns were written to
  columns `classification_tags` does not have, and the failure was swallowed,
  while the wizard told the admin the columns would be redacted. They are now
  recorded as catalog classifications, keyed by schema, table and column so a
  table name containing a dot is no longer split in the wrong place, and the
  wizard says what a tick does: it marks the column for review and role
  authoring, while redaction comes from the PII scanner and from policies.

## 1.0.0-rc.8 - 2026-09-24

### Added

- **Agents can ask what they are allowed to do.** `interlock_describe_access`
  returns, per granted source, the actions the caller's own source roles allow
  and deny. Until now `interlock_list_sources` gave a name and a type and
  nothing about authority, so the only way for an agent to learn a boundary was
  to cross it and read the refusal - a wasted turn per boundary, and a signal a
  weaker model reads as a malfunction worth retrying.

  The summary is built from the same statements the evaluator decides from, so
  it cannot drift from enforcement. It describes only the caller's own
  authority: a source the caller holds no grant on is omitted rather than
  reported as empty, so the tool discloses neither another identity's access nor
  the existence of an ungranted source. Policy is still evaluated per request,
  so an allowed action is permission to attempt rather than a guarantee, and the
  tool description says so where an agent will read it.

  Without a resolved identity it refuses with 403 rather than returning an empty
  object, which would read as "you may do nothing". It is audited against
  `__registry__` like `interlock_list_sources`, and it carries no deprecated
  `agentgate_*` alias because there is no legacy surface to be compatible with.
  Landing it now is deliberate: the compatibility policy allows new tools in a
  minor release, so after `1.0.0` this would have had to wait for `1.1.0`.

### Fixed

- **The console's source-role dry-run was unreachable.** The "Dry-run /
  explain" panel posts to `/dashboard/data-sources/{source_id}/roles/dry-run`,
  and `/roles/{role_id}` was registered first with an unconstrained parameter,
  so Starlette matched `dry-run` as `role_id` and the request died as `422
  unable to parse dry-run as an integer`. The whole explain feature was dead in
  the shipped console - the one surface that answers "what can this identity
  actually do on this source", which the operator guide points auditors at. The
  parameter is now `{role_id:int}`, and a route test asserts both that the
  literal path reaches its own handler and that a numeric id still reaches the
  update handler.

## 1.0.0-rc.7 - 2026-09-24

### Fixed

- **`make e2e-up` works again, on an S3 upstream that is still published.**
  MinIO withdrew its container images from public distribution - quay.io now
  answers `401 Requires authentication` for the repository and Docker Hub
  reports it absent - so the e2e stack failed at image pull on every branch and
  on `main`, before a single test ran. localstack was tried first and is
  license-gated: every published tag, `latest` included, exits 55 demanding an
  auth token.

  The stack now runs `adobe/s3mock` as `s3-upstream`. It creates its buckets at
  startup, so the MinIO client container is gone, and the discovery fixtures are
  uploaded by `tests/e2e/support/seed.py` - which also means a remote S3
  endpoint can be seeded the same way. `e2e-down` gained `--remove-orphans`,
  because a renamed service leaves its old container holding the published port.

  The S3 feature-status evidence says plainly that local certification now runs
  against a mock rather than a real object store, which makes `tests/live` the
  load-bearing evidence for that connector.

- **A malformed MCP tool call is answered as one.** `interlock_query` without a
  `sql` argument reached the governance layer, where the empty statement made
  the permission request derive `db.execute_raw` on `*`. No correctly scoped
  source role allows that, so the agent was told "Source role denied: No source
  role permission allowed every requested resource" and its operator went
  looking at role configuration for what was a missing argument. It now returns
  `400 <tool> requires sql` before any role is evaluated or any upstream is
  touched. Both required-argument errors also quote the tool name the caller
  used rather than the deprecated `agentgate_*` alias it is canonicalised to.

- **The console reports the release it is running.** The sidebar carried the
  literal `v1.0.0-rc.1` from the first release candidate through rc.6, and
  `create_app` carried a second copy of the same literal, so a deployment told
  every operator it was five releases older than it was.

  The distribution's own metadata is now the single source: `__version__` reads
  it through `importlib.metadata`, `release_version()` converts packaging's
  `1.0.0rc6` back to the `1.0.0-rc.6` form the tag and chart use, and the
  sidebar and the FastAPI app version both read from there. `version` in
  `pyproject.toml` moves to `1.0.0rc6`, which is what the sidebar now shows.

  The MCP handshake's `serverInfo.version` carried the same literal, which is
  the copy agents read on every `initialize` and in the `_meta` of every tool
  result - found while walking an agent through key issuance against the live
  deployment.

  A release cannot go out stale again: the release preflight compares the tag
  to the packaged version and fails in seconds if they disagree, before any
  gate runs or anything is published. The contract documents keep citing
  `1.0.0-rc.1` deliberately - that is the release whose public surface they
  froze - and a test scans the shell template, the app factory and the MCP
  adapter so they cannot drift again.

## 1.0.0-rc.6 - 2026-09-23

### Security

- Raised three transitive dependencies past advisories published since the last
  release: `anyio` 4.13.0 to 4.15.1 (CVE-2026-63374, CVE-2026-64847),
  `gitpython` 3.1.59 to 3.1.62 (PYSEC-2026-3982, PYSEC-2026-3983,
  PYSEC-2026-3984) and `soupsieve` 2.8.4 to 2.10 (CVE-2026-85999,
  CVE-2026-86000). `anyio` is a core runtime dependency; the other two are
  reached through the ingestion and repository-connector extras. No version
  floor is added because none was needed - the resolver already prefers the
  fixed releases, and `uv.lock` pins them. `pip-audit` reports no known
  vulnerabilities for the development set or for
  `requirements-production.txt`.

### Fixed

- **Detail pages stop painting over themselves.** Identity, data source, audit
  event, write safety and ingestion job detail laid their sections into
  `repeat(auto-fit, minmax(300px, 1fr))`, so a wide screen gave each section a
  ~300px track. A grid item is not sized by its widest child, and nothing reset
  that, so the identity page's grants table dropped its Revoke column on top of
  the API Key panel and the API Key copy ran under the panel beside it.

  Detail pages are now one stack of full-width sections. Facts read as a
  two-column table, the 24-hour numbers as a single row with column headers
  instead of stacked tiles, Top Sources as a table, and every table - including
  the identity grants table, which had none - sits in the wrapper that scrolls.
  Cards also carry `min-width: 0`, so no card can be stretched by its content
  again. Role badges wrap inside their cell rather than widening the identities
  table.

  The browser certification never opened a detail page, and its clipping check
  only inspects containers that hide their overflow, so neither saw this. It now
  walks identity, audit event, write safety and ingestion job detail, and a new
  assertion fails when any element paints outside the panel that owns it.

## 1.0.0-rc.5 - 2026-09-12

### Fixed

- **A deployment can be upgraded to a newer release in place.** The Gateway
  StatefulSet's `volumeClaimTemplates` carried the full label set, including
  `helm.sh/chart` and `app.kubernetes.io/version`, which the release workflow
  stamps per release. That field is immutable, so every release-to-release
  `helm upgrade` was refused and rolled back; only same-release upgrades worked.
  Found on the first rc.3 to rc.4 in-place upgrade of a test deployment. The pre-upgrade
  migration Job had already run by then, which is why a failed upgrade can still
  leave the schema at the newer head.

  The claim template now carries only stable labels, pinned by a rendered chart
  test. `deploy-oci-release.sh` carries existing deployments across: while the
  live claim template still has a per-release label it deletes the Gateway
  StatefulSet object with `--cascade=orphan` before upgrading, so the pod and
  audit spool volume are kept and adopted. The upgrade runbook documents the step
  for operators who do not use the script.

## 1.0.0-rc.4 - 2026-09-12

### Fixed

- **The console stores JSON as objects, and its Data Sources pages load.**
  The control-plane pools register a jsonb codec that serialises parameters,
  and the console serialised them first, so every source, identity, role,
  role permission and policy it wrote was stored as a JSON *string*. The Data
  Sources list and every source's detail and edit pages then returned 500
  because connector lookup received a string. The grant service, cache
  dependency writer, schema cataloger and discovery indexer made the same
  mistake. Found on a live test deployment; unit tests missed it because their fake
  pools have no codec, and no e2e test created a source through the console.

  Writers now pass Python objects. Migration `015_repair_double_encoded_jsonb`
  converts JSON-string values back into the objects they encode, touching only
  strings whose text is a JSON object or array, so it cannot fail an upgrade on
  a value it does not understand. A guard test fails the build if any call
  binds a jsonb parameter to `json.dumps(...)` again.

## 1.0.0-rc.3 - 2026-09-12

### Changed

- **BREAKING: in production, a PostgreSQL upstream must verify its certificate
  on every protocol - `sslmode: require` is now refused too.** `require`
  encrypts without checking the server certificate, and it passed the
  production gate. That left the same stored source judged two ways: asyncpg's
  `require` with no `root.crt` connects without verification, so MCP and HTTP
  reached it unverified, while the PostgreSQL wire path built a default
  verifying context and checked it. The refusal message already promised
  "Verified upstream PostgreSQL TLS is required"; the gate now means it.
  Production accepts only `verify-full` or `verify-ca`, with the CA supplied
  through `ssl_ca`, `ssl_ca_ref` or `sslrootcert`. `prefer` and `allow` are
  refused for the same reason. Development is unaffected.

  The same judgement now applies at every point an operator meets it.
  `POST` and `PUT /api/data-sources` refuse to store such a source with 422
  and the gate's own message, rather than accepting it and failing at the first
  agent query - on a live deployment the API had accepted one with 201. And Test
  Connection, which never consulted the gate and so reported healthy a
  configuration production refuses, now refuses before making any network
  contact, on the API and console routes alike. One helper,
  `allows_insecure_upstream_tls`, derives the policy for both apps and those
  routes, so they cannot drift apart again.

  **If a production PostgreSQL source uses `sslmode: require`, `prefer` or
  `allow`, change it to `verify-full` and supply the CA before upgrading.**

### Fixed

- **An agent is told why a source cannot be used.** The gateway knew - the
  source's circuit was open, it was disabled, its upstream TLS was refused, or
  no source had that id - and logged it, but an MCP agent received an opaque
  `tool execution failed` and a PostgreSQL client saw its connection dropped.
  The operator had to read gateway logs to learn what a single readable error
  would have said.

  `interlock_query` and `interlock_describe_source` now return that reason: a
  handshake-dialect client gets an `isError` result with
  `structuredContent.status` of `unavailable` or `not_found`, the modern
  dialect a JSON-RPC error carrying it, and the legacy route a `503` or `404`.
  Every other failure stays opaque, because a raw upstream exception can carry
  addresses. On the PostgreSQL wire a refused upstream is now a FATAL `08004`
  with the reason and an unreachable one a FATAL `08006`, sent before
  authentication completes, where the protocol still allows it.

- **The console can register and edit a PostgreSQL source that production
  will serve.** None of the console's write paths - the new-source form, the
  source wizard, or the edit page - could set `sslmode` or a CA, and none
  consulted the production TLS gate. A PostgreSQL source registered through
  the console in production therefore saved cleanly and was then refused on
  every protocol, and the only fix was the API.

  All three now carry a **TLS mode** (defaulting to `verify-full` on the form
  and wizard) and a **CA certificate** field, and refuse at save, with the
  gate's own message, a posture production would refuse at connect. The edit
  page removes any other spelling of the field it writes - `ssl` outranks
  `sslmode` - so an edit cannot leave the old posture quietly in effect, and
  a stored mode the console would not suggest is still offered, so saving an
  unrelated field does not change it. The wizard's schema introspection now
  connects with the configured TLS and applies the same gate first; it used to
  connect with no TLS argument at all.

- **A configuration-only `helm upgrade` now reaches the running pods.** The
  chart delivers configuration through `envFrom`, which a container reads once
  at start, and nothing on the pod templates changed when the rendered ConfigMap
  did. So an upgrade that changed only configuration rewrote the ConfigMap,
  reported success and rolled nothing; verified live, a changed value stayed
  invisible to the running gateway until a manual restart. The Gateway, Admin
  and Worker pod templates now carry a `checksum/config` annotation computed
  from the rendered ConfigMap, so such a change rolls them. Secrets are
  deliberately excluded: production Secrets are external to the chart, and
  hashing the ones it can create would put a digest of inline credentials in
  pod metadata. Rotating a Secret still needs a `kubectl rollout restart`.

- **The runtime image carries Debian security fixes as soon as they are
  published.** The image took its OS packages from whatever the
  `python:3.12-slim` base shipped, and that tag is rebuilt upstream on its own
  schedule. When Debian published fixes for gzip, pcre2, sqlite and perl -
  three CRITICAL and nine HIGH - the base did not yet carry them, so the CI
  supply-chain gate began failing with no change to the image definition, and
  release builds, which pin an older base digest, would have shipped them. The
  runtime stage now runs `apt-get upgrade` before installing its one package.

## 1.0.0-rc.2 - 2026-09-11

### Added

- **An identity's source-role grants can be changed without re-keying the
  agent.** Grants were writable in exactly one place - the identity *create*
  handler - and no route updated them afterwards, so the only way to give an
  existing agent access to a newly registered source was to delete the identity
  and recreate it. Creation mints a fresh API key, shown once, so every
  deployed client holding the old key stopped working. For a product whose
  premise is governed access to data sources, "you cannot grant an existing
  agent access to a new source" was a gap rather than an inconvenience.

  Three routes make a grant a thing in its own right:

  ```
  GET    /api/identities/{id}/source-role-grants     # ?include_revoked=true
  POST   /api/identities/{id}/source-role-grants
  DELETE /api/identities/{id}/source-role-grants/{grant_id}
  ```

  and the identity detail page gains an add form and a revoke control. Both go
  through the same service, so the console and the API cannot drift.

  Little of this is new capability so much as a write path for what the schema
  already described. Migration 007 gave the table an addressable primary key,
  `enabled`, `expires_at` and `granted_by`, and both read paths already filter
  on `enabled` and `expires_at` - so a withdrawal takes effect on the agent's
  very next request, the authenticator revalidating against Postgres each time
  and caching no grants. `granted_by` had never been written by any code path,
  which meant "who gave this agent access, and when" was unanswerable; it is
  now recorded from the acting administrator's session, and both operations
  write a strict Admin audit row.

  Three deliberate behaviours. A revoke disables the row and never deletes it,
  because an incident review asks whether an agent *ever* held access. `POST`
  is idempotent, restating the terms of a grant the identity already holds.
  And a role belonging to a different source is refused with 422 instead of
  stored - the read path joins on both `role_id` and `source_id`, so such a
  grant would have been kept happily and then never matched, reading as
  success while behaving as no grant at all. That last hole existed on the
  create path too, where a supplied `role_id` was used unchecked, and is now
  closed there as well.

### Changed

- **BREAKING: in production, an unencrypted or unverified PostgreSQL upstream
  is now refused on every protocol, not just on the PostgreSQL wire.** The
  production gate lived inline in the PG-wire listener and was handed only to
  it, so the connector path - MCP and HTTP - had no equivalent check at all. A
  source registered `sslmode: require` with `verify_ssl: false` served rows
  happily over MCP, and passed the admin's Test Connection button, while every
  PG-wire connection to that same stored source was refused. The stricter
  control existed; it simply was not applied uniformly, so an operator could
  satisfy themselves that a source worked while it was unverified everywhere it
  did connect.

  Both paths now consult one `upstream_tls_refusal`, and report the same
  wording. Nothing changes outside production, where an upstream with no TLS is
  still permitted for local development. The check is scoped to PostgreSQL
  sources, since it reads the `ssl`/`sslmode` fields a database upstream
  declares - object storage and SaaS connectors carry none and must not be
  refused for their absence.

  **If you run in production with `verify_ssl: false`, or with no TLS mode set,
  those sources will stop serving on MCP and HTTP as well.** That is the point
  of the change, and the fix is to configure the upstream CA - which the same
  release makes possible for the first time, since `ssl_ca` was previously
  ignored on that path entirely.

### Fixed

- **A source's `ssl_ca` now reaches the TLS handshake on every protocol.**
  `_ssl_argument` handed asyncpg the bare mode string and never looked at the
  configured CA, so asyncpg fell back to libpq's default trust store at
  `~/.postgresql/root.crt` - a path that does not exist in the published image,
  which runs as uid 1000 with an emptyDir home. `sslmode: verify-full` could
  therefore not work in a container at all: every MCP query against such a
  source failed with a root-certificate error, surfaced to the agent as an
  opaque `tool execution failed` that never mentioned TLS, while the PG-wire
  path read the same stored field and connected. A verifying mode with a CA
  configured now returns a real `SSLContext` built from it; `verify-ca` leaves
  hostname checking off, an explicit `verify_ssl: false` still wins, and an
  unreadable CA raises rather than quietly connecting on the mode string.

- **The strict config validator accepts every spelling the connect path
  honours.** `read_connection_field` resolves each `CONFIG_ALIASES` group
  identically, but the strict allowlist was a separate hand-maintained tuple
  and had drifted from it: `POST`/`PUT /api/data-sources` answered 422 for
  `sslrootcert` - the libpq parameter name, and the one a PostgreSQL operator
  is most likely to type - while accepting the less familiar `ssl_ca`. The
  allowlist is now derived from the alias table, so a group is widened only
  when one of its members was already permitted and adding a spelling cannot
  reintroduce the gap. Genuinely unknown fields are still rejected.

- **A repository connector no longer loses every asset because one listing is
  unreadable.** `_safe_iter` exists to make optional listings non-fatal, but it
  guarded only the call that produces the listing. PyGithub and python-gitlab
  both return a lazy paginated object, so the HTTP request - and any 403 from a
  least-privilege token - happens on the first iteration instead. A token
  scoped to read a repository's metadata, contents, issues and pull requests,
  but not its Actions, therefore failed the entire sync with
  `Resource not accessible by personal access token`, discarding the five
  surfaces it *could* read. The call and the iteration are now guarded
  together, whatever was read before a mid-listing failure is kept, and the
  skipped listing is logged at warning rather than debug, because the caller
  silently returns fewer assets than the upstream holds.

- **A repository listing limit of `0` now means none.** The five limits were
  read as `int(config.get(key) or DEFAULT)`, so zero - being falsy - selected
  the default and a listing could not be switched off, which is the obvious
  remedy when one of them is unreadable. It was type-dependent too: `0` took
  the default while `"0"` did not. Both spellings now agree.

- **GitHub file contents can be fetched without pinning a `ref`.** The adapter
  passed `ref=None` when none was configured, and PyGithub 2.x validates that
  argument with `is_optional`, which accepts a string or its `NotSet` sentinel
  and rejects `None`. Every file asset failed at materialisation with a bare
  `AssertionError: None` while the repository and its issues indexed normally,
  so a source looked healthy with its code silently missing. The argument is
  now omitted rather than passed as `None`.

- **PII redaction records what it masked.** `audit_log.redaction_stats` is
  where an operator is told to look to confirm that a response was redacted,
  and it was written only by the HTTP `redact_columns` path - so on MCP, the
  path agents actually use, it was always null while redaction was in fact
  happening. `pii_types` was empty for the same class of reason: scanner
  detections were filtered with `isinstance(d, dict)` against a `PIIMatch`
  model, which is not a dict and whose field is `entity_type`, so no detection
  ever matched even though `pii_detected` was set. Both are now populated from
  the scanner path, with per-entity counts and a scanner-failure tally; a
  request that triggered no redaction still records nothing rather than a
  misleading zero-filled object.

## 1.0.0-rc.1 - 2026-09-09

### Security

- Constrained `httpx2>=2.12.0` for CVE-2026-84379, CVE-2026-84380 and
  CVE-2026-84382. It is reached only through `mcp` in the `mcp-certification`
  extra, so it is a test-and-tooling dependency: it does not appear in
  `requirements-production.txt` and is not in the published image, and the
  runtime's own HTTP client is `httpx`, which is unaffected. `pip-audit`
  reports no known vulnerabilities.

- Raised three dependency floors past their advisories, each inside the
  existing version ceiling: `pip>=26.2` (PYSEC-2026-3721),
  `transformers>=5.10.0` (CVE-2026-9856) and
  `snowflake-connector-python>=4.7.1` (CVE-2026-15925). `pip-audit` reports no
  known vulnerabilities.

- Only the Snowflake connector reached a shipped image. `transformers` belongs
  to the `ml` extra and `pip` is development tooling; neither appears in
  `requirements-production.txt`, so the Gateway and Admin runtime images were
  never exposed to the other two.

- The Snowflake connector moved 4.5.0 -> 4.7.3, seven minors, and its adapter
  has only mocked coverage. Its unit tests pass, but nothing here proves the
  new client against a real Snowflake account - `tests/integration/test_snowflake_live.py`
  is what a credential holder should run before trusting it.

### Added

- **A queued write now tells a human about itself.** `ApprovalQueue.submit`
  inserted a row and raised; nothing emitted an event, so the only way to
  learn that an agent's write was waiting was to be looking at the Admin
  console - and the queue expired entries after fifteen minutes.

  `ApprovalQueue` now emits `pending`, `approved`, `rejected`, `expired` and
  `failed` events to a Slack sender wired into both the Gateway and Admin
  lifespans, over an incoming webhook or a bot token. Credentials arrive as
  secret references (`env://` and friends); a literal, an unresolvable
  reference, or one that resolves to an empty string refuses to start, because
  a notifier that silently never delivers is worse than a boot failure.

  Delivery is best-effort and cannot affect the request that triggered it:
  `notify()` returns after scheduling, retries are bounded, and every failure
  is logged and dropped. The governance decision is already made and recorded
  by then. Messages carry a statement fingerprint redacted more aggressively
  than the approvals API - every numeric literal is removed, not only long
  runs - plus a link to the approval page. Interactive Slack buttons are
  deliberately not offered: they would require a public inbound endpoint with
  signature verification and a Slack-user-to-admin authorization mapping.

- **`approvals.expiry_seconds` replaces a hard-coded 900.** The default is
  unchanged, but fifteen minutes predated any way of being told an approval
  existed. The expiry sweep now uses `RETURNING`, so each lapsed approval can
  be notified exactly once even with several gateways sweeping concurrently.

- **Helm: the values a real deployment needs.** Every one of these was found by
  trying to deploy InterLock to a managed Kubernetes cluster against real data
  sources, where the chart could render a healthy-looking release that could
  not actually work. `imagePullSecrets` (attached to every pod including the
  pre-install migration Job, which pulls the same image); `extraEnv` and
  `extraEnvFrom` on the Gateway, Admin and Worker, which is how `env://`
  connector references are supplied - without them every source configured
  with a secret reference failed closed, and the Admin needs them too because
  it resolves the same references when probing a source;
  `admin.bootstrapPasswordExistingSecret`, without which there was no way to
  create the first administrator; `redis.existingSecret`, because a managed
  Redis carries its password inside the URL and that URL was rendered into a
  plaintext ConfigMap; `gateway.service.annotations`;
  `gateway.pgService.enabled`, which publishes the PostgreSQL wire port on its
  own Service so exposing it does not also publish HTTP past the Ingress; and
  `admin.ingress.*`, because the chart's Ingress routes only the Gateway while
  admin cookies are unconditionally `Secure`, leaving the console unreachable.
  All default to off, so an existing release upgrades unchanged.

### Changed

- **GitHub attestation steps are skipped while the repository is private.**
  GitHub's attestation store is a paid feature for private repositories, and
  the first tagged release failed there after the image had already been built,
  signed and pushed - taking the Helm chart, the release manifest and the
  GitHub Release down with it.

  The four `actions/attest` steps are now conditional on the repository being
  public, so they resume by themselves when it is, with no workflow edit to
  remember. They are deliberately *not* `continue-on-error`: swallowing the
  failure would keep passing once the step should work, so a real regression
  in provenance would look exactly like today. A test pins that distinction.

  This is a change of storage and verification path, not a loss of provenance.
  Every release still publishes a cosign signature over the image digest,
  verifiable against the workflow's OIDC identity through the Sigstore
  transparency log, plus SLSA v1 provenance and an SPDX SBOM attached to the
  image in the registry - and that provenance names the released commit. The
  release manifest now records which mechanisms ran under `provenance`, so a
  consumer can tell which verification commands will work instead of guessing.

### Fixed

- **`POST /mcp` rejected every mainstream MCP client.** The endpoint accepted
  only protocol version `2026-07-28` and answered `initialize` with `-32601`,
  while Claude Code, Claude Desktop and the official SDKs open with
  `initialize`, speak `2025-03-26`/`2025-06-18`/`2025-11-25`, and send none of
  the mirrored `Mcp-Method`/`Mcp-Name`/`_meta` headers that dialect requires.
  For a proxy whose own `agent_type` enum lists `claude_code`, this meant no
  supported agent could connect.

  The endpoint now serves both dialects, chosen per request. `2026-07-28`
  keeps its mirrored-header requirements unchanged; handshake-era clients
  negotiate through `initialize`, may omit the version header (the 2025-06-18
  spec says to assume `2025-03-26` when it is absent), and get `ping` as a
  bare empty object because deployed clients validate that strictly. No
  session id is issued in either dialect. Verified against the installed
  `claude` binary: `claude mcp list` reports the server Connected.

- **A write held for approval came back malformed over `POST /mcp`.** The 202
  decision flowed through as a JSON-RPC success whose body was `{"error": ...}`
  with no `content` array, so a strict client rejected it and the agent never
  learned an approval existed. It is now a tool result with `isError: false`
  and `structuredContent` carrying `approval_id` and `approval_state`, plus
  text stating the statement was not executed and must not be retried -
  `ApprovalQueue.submit` does not deduplicate, so a retry would enqueue a
  second approval for one intended write. `/mcp/tools/call` returns the same
  two fields alongside `error`, matching the HTTP proxy.

- **Governance refusals were unreadable to handshake-era clients.** A denial,
  rate limit or auth failure was a JSON-RPC error with a 4xx status, which the
  TypeScript SDK surfaces as "Error POSTing to endpoint" with the reason
  buried. In the handshake dialect these are now tool results with
  `isError: true` and `structuredContent: {status, code, message}`. The modern
  dialect keeps JSON-RPC errors, which is its contract.

- The wheel shipped migrations to `onyx/migrations` while the runner resolved
  them next to the package, so a pip-installed deployment could not find them.

- `aquasecurity/trivy-action@0.28.0` is not a tag in that repository, so the CI
  image-scan step could never resolve; it is now pinned to the `v0.28.0`
  commit.

- A unit test depended on an absolute path from a developer's machine, and two
  more failed instead of skipping without optional extras; the unit suite is
  now green with `--extra dev` alone.

### Licensing

- **The LGPL-3.0 repository connectors now ship in the published image.**
  `PyGithub` and `python-gitlab` move into the `connectors-repo` extra's
  consumers, `production` and `production-worker`, and therefore into
  `requirements-production.txt` and the container image.

  They were excluded so the image carried no copyleft dependency. The cost was
  that the GitHub and GitLab connectors could not work in the only image most
  people run - the adapters raise `PyGithub is not installed` - so a
  documented capability was unusable as shipped. Including the libraries was
  judged the more honest position.

  InterLock's own license is unchanged: it remains Apache-2.0, and depending on
  an LGPL-3.0 library does not place it under the LGPL. `NOTICE` now records
  which libraries ship, that they are unmodified and dynamically imported, and
  how a recipient may replace them - the obligation the LGPL actually imposes.
  CI continues to deny GPL-2.0, GPL-3.0 and AGPL-3.0; only LGPL-3.0 was
  relaxed, and a test fails if that denial and the shipped extra ever
  contradict each other.

  Anyone needing a copyleft-free build can install `production` without
  `connectors-repo`; only those two connectors are lost, and they fail closed.

### Renamed to InterLock

- **Breaking (source):** the Python import namespace is now `interlock`
  (`python -m interlock.gateway` / `.admin` / `.worker`), and the package
  installs console scripts `interlock-gateway`, `interlock-admin`,
  `interlock-worker`, and `interlock-migrate`.

- **Deprecated:** `ONYX_*` environment variables and `ONYX_CONFIG_PATH` are
  mapped onto their `INTERLOCK_*` equivalents with a `DeprecationWarning`.
  When both names are set the `INTERLOCK_*` value wins. Removal no earlier
  than `1.2.0`, alongside the `agentgate_*` MCP tool aliases.

- Admin session and CSRF cookies are now `interlock_admin_session` and
  `interlock_admin_csrf`; existing browser sessions are invalidated once.

- The Helm chart moved to `deploy/helm/interlock`, and the container user and
  home directory are now `interlock` / `/home/interlock`.

- Unchanged by design: PostgreSQL `LISTEN`/`NOTIFY` channels, Redis pub/sub
  channels, the `agentgate_seed_source_role_permissions` function, and the
  reference stack's `onyx` PostgreSQL role and database. See
  `docs/contracts/compatibility-policy.md`.

### Cloud certification

- Fixed a deployment-blocking chart bug: the admin container inherited the
  `127.0.0.1` default bind address, so in Kubernetes the kubelet could never
  reach its readiness probe and `rollout status` hung. The chart now sets
  `INTERLOCK_ADMIN__HOST=0.0.0.0`.

- Added `deploy/helm/values.cloud.example.yaml`, documenting every value an
  operator must supply, all six `existingSecret` references, and the five
  Secrets plus StorageClass the chart requires but does not create.

- The cloud-certification workflow now certifies instead of only deploying: it
  bootstraps an ephemeral in-cluster PostgreSQL and Redis with generated TLS,
  asserts `/ready` on gateway and admin, runs the governed-core end-to-end
  subset through port-forwards, re-runs `helm upgrade`, and uploads redacted
  evidence even when the run fails.

- Terraform state moved to a remote S3-compatible backend with a per-run key,
  so a cancelled runner no longer orphans a cluster beyond recovery. This
  raises the required Terraform version to 1.11.

- Added `make terraform-validate`, `make actionlint`, and `make check-infra`
  for offline validation, wired into the local release gate.

### Repository

- Apache-2.0 `LICENSE` now carries a copyright line, and a `NOTICE` documents
  third-party and copyleft components.

- Added `CODEOWNERS`, `dependabot.yml`, a Code of Conduct with a reporting and
  enforcement process, and contributor setup instructions with a lightweight
  install path.

- All GitHub Actions in `ci.yml` are SHA-pinned; `make lint` and the CI format
  check now cover `tools/` and `scripts/`.

- The internal audit and certification trail was removed from the repository.
  Evaluator-facing documents moved to `docs/`, and certification evidence is
  generated into `build/` rather than committed.

- The secret scanner no longer hard-codes internal hostnames, covers more
  credential shapes, reports line numbers, and supports a line-scoped
  `# secret-scan: allow` pragma for synthetic fixtures.

- New guards: migration checksums are pinned, and relative links in tracked
  markdown must resolve.

### Prior work

- Public-beta launch remediation in progress.

- Public launch blockers are tracked in `docs/release-readiness.md`.

- Apache-2.0 licensing, local release evidence, supply-chain gates,
  production-safe Helm/Docker validation, and local Final Boss certification
  are complete. Remaining release blockers are hosted CI artifacts, automated
  cloud deployment, published signing/provenance, the final manual keyboard
  walkthrough, and live-source certification decisions.

- Added release-engineering gates for locked production requirements, SBOM/CVE
  evidence, secret/security/audit CI checks, Docker image build/scan, Helm
  production defaults, and `make final-boss-local`.

- Remediated production dependency CVE findings in the locked public-beta
  export; no active production dependency CVE waivers are accepted as of
  2026-07-17.

- Remediated all-extras/local-security dependency findings so the bare
  `pip-audit` gate is clean after `make install`.

- Made strict audit durability fail closed across HTTP, MCP, and PostgreSQL;
  PostgreSQL now admits the final audit event before flushing cached, simple,
  Sync, or Flush success frames to a client.

- Hardened persisted private-egress booleans so legacy string values such as
  `"false"` and `"0"` cannot enable private network access in HTTP proxy,
  source probe, or approval execution paths.

- Removed Admin inline styles and `style-src 'unsafe-inline'`, masked alert
  notification targets, added skip/live-region and modal focus management, and
  made operational statuses visibly textual instead of color-only.
