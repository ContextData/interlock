# InterLock

**Control what AI agents can read and change in your databases and APIs.**

InterLock is an open-source, self-hosted gateway for **platform engineers, AI
application developers, and security teams** connecting AI agents to company
data. It gives you one place to manage agent permissions, redact supported
sensitive data, require human approval for risky writes, and audit activity.

When an agent connects directly to a business system, its credentials determine
what it can access. Teams also need to control which requests it can make, what
information comes back, and which changes require a person's approval.
InterLock applies those controls to requests routed through its gateway.

Agents connect through PostgreSQL, HTTP, or the Model Context Protocol (MCP).
InterLock checks each request before it reaches the registered data source.

**Status:** InterLock is in release candidates for 1.0. Capabilities and
certification vary by connector and protocol. See
[feature status](docs-site/src/content/docs/reference/feature-status.md)
and the
[connector support matrix](docs-site/src/content/docs/reference/connector-support-matrix.md).

## Who it is for

- **Platform engineers** providing a shared way for multiple agents to access
  databases, APIs, and connected services.
- **AI application developers** building agents that need business data and
  controlled permissions to act on it.
- **Security teams and data owners** deciding what each agent may access,
  reviewing sensitive changes, and investigating activity.

## What problem does it solve?

An agent may need to answer questions about customers without seeing their
email addresses, deleting records, or making changes without review.

With the appropriate source roles and policies configured, InterLock can
handle that workflow like this:

| Agent request | InterLock's response |
| --- | --- |
| A read-only agent queries permitted customer records. | Allow the query and replace detected email addresses with `[REDACTED:EMAIL]`. |
| That agent attempts to delete an order. | Deny the request before it reaches the database. |
| An agent with update permission changes an order using a supported SQL `UPDATE ... WHERE ...` statement. | Under the default write-risk handling, hold the update for a reviewer to approve or reject. |
| An operator investigates those requests. | Show the identity, source, decision, and outcome in the audit log. |

This gives teams a shared place to control agent access as they add more
agents and data sources.

## How it works

```text
AI agent or application
          |
          | PostgreSQL / HTTP / MCP
          v
      InterLock
          |
          | Access checks, policy, write approval,
          | response redaction, and audit
          v
Databases, APIs, and connected services
```

1. **Register a source.** Configure the connection InterLock uses to reach a
   database, API, or supported service.
2. **Give each agent an identity.** Issue an InterLock API key and grant source
   roles describing its permitted actions and resources.
3. **Apply policies.** Allow the traffic you expect, and add restrictions such
   as denials, rate limits, response-column redaction, or write-risk caps. A
   policy cannot grant access that a source role does not allow.
4. **Connect the agent to the gateway.** Configure its endpoint and credentials,
   or add InterLock as an MCP server.
5. **Review activity in the console.** Manage access, inspect audit records,
   and approve or reject queued writes.

Source-role authorization and policy checks must both allow a request; a
request that no policy allows is denied. Allowed writes are then classified by
risk:

- **Low risk** (for example an SQL `INSERT`, or an HTTP `POST`) runs straight
  away.
- **Medium and high risk** (for example `UPDATE` or `DELETE`, DDL, or an HTTP
  `PUT` or `DELETE`) enters the approval queue, and runs once only if a
  reviewer approves it.
- **Above a policy's write-risk cap** is denied outright.

InterLock connects upstream using credentials registered for the source.
Agents authenticate to InterLock using their own identities.

## What you can manage

| Capability | What it gives you |
| --- | --- |
| Agent identities and API keys | Attribute requests to individual agents; rotate or revoke their access. |
| Source roles | Limit actions and resources per source, including table and column permissions for supported SQL paths. |
| Policies | Apply additional restrictions, rate limits, redaction rules, and write-risk caps. |
| Human approval | Review supported risky writes before they execute. |
| Sensitive-data redaction | Mask recognized personal information and apply named-column redaction on supported response paths. |
| Audit records | Inspect agent requests, access decisions, write approvals, and administrative actions. |
| Catalog and discovery | Inspect supported source structures and let agents discover content within their access scope. |
| Read caching | Reuse eligible responses with cache scoping and invalidation controls. |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs-site/src/assets/screenshots/dark/overview.png">
  <img alt="InterLock admin console showing service health and recent activity" src="docs-site/src/assets/screenshots/light/overview.png">
</picture>

## Connections and data sources

Agents connect through three interfaces:

| Interface | How it is used |
| --- | --- |
| **MCP** | Connect an MCP client to the gateway's `/mcp` endpoint for supported query and discovery tools. |
| **PostgreSQL wire protocol** | Point a compatible PostgreSQL client at the gateway, use the registered source ID as the database name and the API key as the password. |
| **HTTP** | Send requests through the governed proxy for registered HTTP sources. |

Source capabilities differ:

- **PostgreSQL:** governed queries through the supported PostgreSQL protocol
  subset and MCP.
- **MySQL/MariaDB:** governed queries over MCP through the connector; there is
  no native MySQL wire listener.
- **HTTP APIs:** access controls based on methods and paths, with supported
  response processing and write approval.
- **Amazon S3 and DigitalOcean Spaces:** object read and discovery access;
  governed object writes are unavailable in this release.
- **Additional beta connectors:** Slack, GitHub, GitLab, Snowflake, Zendesk,
  OpenSearch, Elasticsearch, Qdrant, Salesforce, Notion, and Google Workspace.
  Available operations and certification vary.

A new installation activates PostgreSQL, MySQL, S3, Slack, GitHub, and HTTP;
an administrator activates the others in the console. For exact capabilities,
activation requirements, and test coverage, see the
[connector support matrix](docs-site/src/content/docs/reference/connector-support-matrix.md).

### Tested agents

Any MCP client using protocol version 2025-03-26, 2025-06-18, 2025-11-25, or
2026-07-28 can connect; InterLock authenticates the API key, not the model. These have been run against
a live deployment through the same checks (a redacted read, a role denial, a
policy denial, a query error, and the audit trail):

- **Run on your machine:** Claude Code, Codex CLI, the official MCP Python SDK,
  and the `google-genai` SDK.
- **Hosted by the model provider:** the OpenAI Responses API, the Gemini
  Interactions API, and the Anthropic API MCP connector. These connect from the
  provider's cloud, so the gateway needs a public address and a publicly
  trusted certificate.

Versions, dates, per-client settings, and known differences are in
[Connect an agent](docs-site/src/content/docs/guides/connect-an-agent/mcp-clients.md).

## Quick start

Run InterLock locally with a sample shop database, then verify an allowed
query, redacted email addresses, and a denied write.

### Prerequisites

- Docker running with Docker Compose 2.24 or later.
- Git and curl.

The basic walkthrough does not require a model-provider API key or a local
PostgreSQL installation.

### Start the local stack

```bash
git clone https://github.com/ContextData/interlock
cd interlock
docker compose --profile quickstart up -d --wait
```

The first run builds the images and may take several minutes.

| Service | Local address |
| --- | --- |
| Admin console | http://127.0.0.1:9090 |
| HTTP and MCP gateway | http://127.0.0.1:3001 |
| PostgreSQL gateway | `127.0.0.1:5434` |

Open the admin console and sign in with username `admin` and password `admin`.
You must choose a new password before continuing.

### Run your first governed query

Follow the
[quick-start walkthrough](docs-site/src/content/docs/get-started/quick-start.mdx)
to:

1. Register the included sample database.
2. Create a policy allowing reads.
3. Create an agent identity with the source's Read role and copy its API key.
4. Query the sample data through the gateway, over the PostgreSQL wire and MCP.
5. Attempt a write that the Read role does not allow.
6. Inspect the requests in **Audit & Costs**.

The walkthrough returns customer rows with email addresses replaced by
`[REDACTED:EMAIL]` and rejects the unauthorized delete.

To connect a real agent next, see
[First governed query from Claude Code](docs-site/src/content/docs/get-started/first-query.md)
or [Connect an agent](docs-site/src/content/docs/guides/connect-an-agent/mcp-clients.md)
for other clients.

The Compose configuration uses development credentials and binds ports to
localhost. For deployment beyond local evaluation, follow the
[production checklist](docs-site/src/content/docs/operations/production-checklist.md).

## Deployment and operating boundaries

InterLock runs a gateway, an admin service, and background workers, backed
by PostgreSQL and Redis. Docker Compose supports local evaluation and single
hosts; a Helm chart supports Kubernetes. Each release is published as a
cosign-signed container image and Helm chart. The release process requires the
chart to pass an automated certification on disposable DigitalOcean (DOKS) and
Amazon EKS clusters; both gates have passed. Step-by-step guides cover a
DigitalOcean Droplet, an AWS EC2 instance, DigitalOcean Kubernetes, and
Amazon EKS.

Your existing systems remain the sources of record. InterLock also stores
configuration, audit records, the source catalog (names and types, never
values), cached responses, and, for sources you enable discovery on, text,
summaries, and embeddings extracted from their content. If you configure the
optional Anthropic or Mistral API keys, workers send ingested document content
to those providers for summaries and OCR.

Before relying on a particular control, account for these current boundaries:

- **Traffic must pass through InterLock.** Direct access using separate
  upstream credentials is outside the gateway's controls.
- **Upstream permissions remain essential.** SQL checks parse submitted
  statements with `sqlglot`. Views, functions, triggers, and database-side
  dynamic SQL can access resources those statements do not name. Use
  appropriately restricted upstream accounts.
- **Redaction varies by protocol.** Named-column policy redaction applies to
  MCP query results and HTTP JSON responses. PostgreSQL wire responses use
  content-based PII scanning, which can miss sensitive values. S3 object
  bodies are not covered by that redaction.
- **Compatibility has a defined scope.** The PostgreSQL listener implements a
  protocol subset; asyncpg is the certified client. Connector availability
  does not imply that every operation has been certified.
- **Some capabilities remain unavailable.** Semantic cache serving is
  disabled, and broad SaaS mutations are outside the current release scope.

Read the
[security model](docs-site/src/content/docs/concepts/security-model.md),
[known limitations](docs-site/src/content/docs/reference/known-limitations.md),
and [feature status](docs-site/src/content/docs/reference/feature-status.md)
for the detailed boundaries and supporting evidence.

## Documentation

Visit the [documentation site](https://interlock.contextdata.dev).

| Goal | Guide |
| --- | --- |
| Understand what it does and does not do | [What is InterLock](docs-site/src/content/docs/get-started/what-is-interlock.mdx) |
| Evaluate InterLock locally | [Quick start](docs-site/src/content/docs/get-started/quick-start.mdx) |
| Configure a real setup | [Setup walkthrough](docs-site/src/content/docs/get-started/setup-walkthrough.md) |
| Connect an agent | [MCP clients](docs-site/src/content/docs/guides/connect-an-agent/mcp-clients.md) |
| Understand the services | [Architecture](docs-site/src/content/docs/concepts/architecture.md) |
| Deploy to Kubernetes | [Deploy with Helm](docs-site/src/content/docs/operations/deploy-with-helm.md), [DigitalOcean Kubernetes](docs-site/src/content/docs/operations/deploy-on-digitalocean-kubernetes.md), [Amazon EKS](docs-site/src/content/docs/operations/deploy-on-amazon-eks.md) |
| Deploy on a single host | [Deploy with Compose](docs-site/src/content/docs/operations/deploy-with-compose.md), [DigitalOcean Droplet](docs-site/src/content/docs/operations/deploy-on-a-digitalocean-droplet.md), [AWS EC2](docs-site/src/content/docs/operations/deploy-on-an-aws-ec2-instance.md) |
| Check compatibility commitments | [V1 contracts](docs-site/src/content/docs/reference/contracts/index.md) |
| See how releases are gated | [Release process](docs-site/src/content/docs/project/release-process.md) |
| Review release changes | [Changelog](CHANGELOG.md) |

Documentation source lives in [`docs-site/`](docs-site).
Run `make docs-build` to build it locally.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance.

Report vulnerabilities privately using [SECURITY.md](SECURITY.md), never in a
public issue.

## License

InterLock is licensed under the Apache License, Version 2.0.
See [LICENSE](LICENSE) and [NOTICE](NOTICE) for third-party components.
