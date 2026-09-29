"""Public-beta feature status registry.

This module is the source of truth for evaluator-facing capability status.
Public docs and Admin labels should not advertise a capability as enabled
unless it has evidence here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FeatureState = Literal["certified", "beta", "disabled", "planned"]


@dataclass(frozen=True)
class FeatureStatus:
    key: str
    label: str
    state: FeatureState
    public_beta: bool
    evidence: str
    limitation: str = ""


# Connectors that ship an adapter but have no source registered in any test
# stack, so nothing exercises source roles, policy, redaction, write safety or
# audit against them. Adapter unit tests are not governance tests, and the
# distinction is the whole point: the claim "Beta" is honest only while it is
# read as "the adapter works", never as "governance is proven".
#
# tests/e2e/test_connector_governance_uniformity.py imports this rather than
# keeping its own copy. Two hand-maintained lists would drift, and drift here
# is silent in exactly the direction that flatters the product.
#
# Slack and Google Workspace are deliberately absent: they are also missing
# from the e2e stack, but tests/live certifies their governance against real
# tenants, so their governance is proven - just not in CI.
GOVERNANCE_UNVERIFIED_CONNECTORS: tuple[str, ...] = (
    "elasticsearch",
    "github",
    "gitlab",
    "snowflake",
    "zendesk",
)


FEATURE_STATUSES: tuple[FeatureStatus, ...] = (
    FeatureStatus(
        key="governance_pipeline",
        label="Gateway governance pipeline",
        state="beta",
        public_beta=True,
        evidence="Unit and E2E coverage exists for source roles, policy, approvals, redaction, and audit foundations.",
        limitation="Final Boss must still prove every protocol and connector path cannot bypass the pipeline.",
    ),
    FeatureStatus(
        key="postgresql_proxy",
        label="PostgreSQL proxy",
        state="beta",
        public_beta=True,
        evidence="Compose and focused SQL governance tests cover source-aware auth, role-scoped SQL, write safety, audit, and redaction paths, and tests/live certifies upstream access, role allow and deny, redaction, write safety, approval gating, and audit against a real managed PostgreSQL.",
        limitation="Policy deny is not independently proven: the blocked identity holds both a blocked source role and a matching deny policy, so a refusal cannot be attributed to policy alone. Client compatibility is certified for asyncpg only.",
    ),
    FeatureStatus(
        key="mysql_connector",
        label="MySQL/MariaDB connector",
        state="beta",
        public_beta=True,
        evidence="Connector tests cover probing, sqlglot parsing, permission requests, and role-scoped execution boundaries, and tests/live certifies upstream access, role allow and deny, redaction, write safety, approval gating, and audit against a real managed MySQL.",
        limitation="Policy deny is not independently proven, for the same fixture reason as PostgreSQL.",
    ),
    FeatureStatus(
        key="http_proxy",
        label="HTTP proxy",
        state="beta",
        public_beta=True,
        evidence="Local mock-upstream tests cover method/path permissions, cache/redaction foundations, approval queueing, and audit.",
        limitation="Circuit-breaker certification remains a Final Boss evidence item. Streaming redaction is no longer deferred: it is covered by tests/e2e/test_streaming_redaction_guarantees.py, including the quoted-newline CSV case that previously evaded it.",
    ),
    FeatureStatus(
        key="mcp_tools",
        label="MCP tools",
        state="beta",
        public_beta=True,
        evidence="Canonical POST /mcp JSON-RPC Streamable HTTP, request-scoped JSON/SSE responses, InterLock-prefixed schemas, source-aware routing, filtering, and audit tests exist. Redaction is applied by every tool that returns rows, not only query. Handshake-era clients (2025-03-26, 2025-06-18, 2025-11-25) negotiate through initialize, and the official python SDK is exercised in both auto and legacy modes.",
        limitation="Legacy routes and agentgate_* aliases remain deprecated through the V1 compatibility window. Claude Code connectivity is verified by an operator step rather than a gate, and adversarial cross-source certification remains a release evidence item.",
    ),
    FeatureStatus(
        key="s3_spaces_connector",
        label="S3 and DigitalOcean Spaces object storage",
        state="beta",
        public_beta=True,
        evidence="Local certification runs against an S3 API mock (adobe/s3mock) and covers probe, list, fetch, discovery, role denial, masking, and disposable write/delete paths; because a mock is not a real object store, tests/live is the load-bearing evidence and certifies upstream access, role allow and deny, and audit against a real bucket.",
        limitation="Governed writes are unreachable, not merely uncertified: S3 declares supports_query=False and supports_proxy=False, so no protocol surface can express an object write and there is nothing to approve or gate. Redaction is column-oriented and does not apply to object bodies.",
    ),
    FeatureStatus(
        key="beta_enterprise_connectors",
        label="Beta read/discovery enterprise connectors",
        state="beta",
        public_beta=True,
        evidence="Slack, GitHub/GitLab, Snowflake, Zendesk, OpenSearch/Elasticsearch, Qdrant, Salesforce, Notion, and Google Workspace have mocked or local adapter tests. Slack and Google Workspace are additionally certified live for read, discovery, role denial, redaction, and audit.",
        limitation="Adapter coverage is not governance coverage. Five connectors have no source registered in any test stack, so that source roles, policy, redaction, write safety, and audit apply to them is untested: elasticsearch, github, gitlab, snowflake, zendesk. Treat as read/discovery beta unless the connector support matrix explicitly lists certified writes.",
    ),
    FeatureStatus(
        key="approval_notifications",
        label="Approval Slack notifications",
        state="beta",
        public_beta=True,
        evidence="ApprovalQueue emits pending, approved, rejected, expired and failed events to a Slack sender wired into both the Gateway and Admin lifespans. Unit tests cover payload redaction, both the webhook and bot-token transports, bounded retries and failure isolation; tests/e2e/test_approval_notifications.py proves a queued MCP write posts exactly one redacted notification and that approve and reject post their outcome.",
        limitation="Delivery is best-effort from the process that enqueued or resolved the approval: a Slack outage never blocks the agent request, and a notification is lost if that process exits before the background post completes. Messages carry a redacted statement fingerprint and a link to the Admin approval page; interactive Slack buttons are deliberately not offered. Email and generic webhook channels, and alert-rule dispatch, remain planned.",
    ),
    FeatureStatus(
        key="semantic_cache",
        label="Semantic cache serving",
        state="disabled",
        public_beta=False,
        evidence="Semantic cache plumbing and scoped tests exist, but production serving is not a public-beta capability.",
        limitation="Keep disabled until semantic lookups are proven to scope by source, identity/team, active grants, policy hash, and invalidation state.",
    ),
    FeatureStatus(
        key="otel_export",
        label="OpenTelemetry export",
        state="beta",
        public_beta=True,
        evidence="Gateway, Admin, and Worker startup paths call the shared OTel bootstrap helper; configured OTLP endpoints export traces/metrics when the optional otel extra is installed.",
        limitation="Defaults to no-op without an OTLP endpoint or optional dependencies; dashboards and collector-backed Final Boss certification remain pending.",
    ),
    FeatureStatus(
        key="source_catalog",
        label="Source catalog",
        state="beta",
        public_beta=True,
        evidence=(
            "Workers record each PostgreSQL, MySQL/MariaDB and Snowflake source's schemas, tables, views and columns, and the buckets and prefixes, channels, repositories, objects and fields, indices and collections of S3, Spaces, Slack, GitHub, Salesforce, OpenSearch/Elasticsearch and Qdrant sources, when it is saved, on Rescan, and on a scheduled refresh, with drift between scans; tests/e2e/test_source_catalog.py covers every save path, two workers sharing the queue, drift against a real upstream table, truncation, and discovery indexing from the catalog. Drift records which roles already reach a new table or column, and an access-analytics page reads the audit trail against the catalog."
        ),
        limitation=(
            "SQL table names resolve through the catalog and column-level rules are enforced on MCP and the PostgreSQL wire, within the limits of parsing: functions, views and dynamic SQL inside the database are not seen, so the mapped role's upstream grants remain the guarantee. Policy redact_columns applies on MCP and HTTP, not on the PostgreSQL wire. The role and policy editors pick from the catalog and warn against it, but never block a save. Non-SQL catalogs are inventory only: agent requests to those sources are not checked against them, and Salesforce records only its configured objects. GitLab, Zendesk, Notion, Google Workspace and generic REST are not catalogued."
        ),
    ),
    FeatureStatus(
        key="rrf_ranking",
        label="Discovery reciprocal rank fusion",
        state="beta",
        public_beta=True,
        evidence="DiscoverySearch fuses vector, full-text, and metadata ranked lists with reciprocal rank fusion; deterministic unit tests cover cross-strategy boosts, raw-score independence, metadata preservation, and tie-breaking.",
        limitation="Category classifier scoping, query-time entity enrichment, and compose-level RRF discovery E2E remain separate Final Boss/backlog items.",
    ),
    FeatureStatus(
        key="category_classifier_enrichment",
        label="Automatic category classifier and entity enrichment",
        state="planned",
        public_beta=False,
        evidence="Explicit category metadata and entity storage exist.",
        limitation="Automatic classifier-driven scoping and query-time entity enrichment are not public-beta capabilities yet.",
    ),
    FeatureStatus(
        key="qdrant_vector_backend",
        label="Qdrant vector backend",
        state="planned",
        public_beta=False,
        evidence="Qdrant is implemented as a governed source connector; cache backend interfaces exist, but nothing on the request path imports them.",
        limitation="Qdrant-as-vector-backend is separate from Qdrant-as-source and is not certified for public beta.",
    ),
    FeatureStatus(
        key="dependency_tracked_cache_invalidation",
        label="Dependency-tracked cache invalidation",
        state="beta",
        public_beta=True,
        evidence="PG, HTTP, MCP, and approval write paths record deterministic cache dependencies, invalidate dependency keys after writes, and publish Redis invalidation events for peer gateway instances. tests/e2e/test_cache_correctness_guarantees.py verifies no serving across identities, sources, or policy versions, and that the write barrier holds.",
        limitation="Semantic cache serving remains disabled for public beta; compose-level multi-gateway certification and load/stampede tests remain Final Boss evidence items.",
    ),
    FeatureStatus(
        key="alerts_notifications",
        label="External alert notifications",
        state="planned",
        public_beta=False,
        evidence="Alert configuration and readiness UI exist.",
        limitation="Alert rules have no scheduler and no dispatcher; a manual evaluation records alert_history only. The Slack sender shipped for approval notifications is the intended transport once scheduled evaluation exists.",
    ),
    FeatureStatus(
        key="deep_pii_scanner",
        label="Deep PII scanner",
        state="beta",
        public_beta=True,
        evidence="Gateway lifecycle wires PIIDeepScanner when pii.deep_enabled is true; ResponseProcessor tests cover configured free-text patterns, contextual deep matches, nested JSON strings, and fail-closed scanner exceptions.",
        limitation="Requires interlock-runtime[pii] optional dependencies and an available Presidio/spaCy runtime; PostgreSQL wire redaction remains fast-tier only.",
    ),
)


def feature_status_map() -> dict[str, FeatureStatus]:
    return {feature.key: feature for feature in FEATURE_STATUSES}


def public_beta_features() -> tuple[FeatureStatus, ...]:
    return tuple(feature for feature in FEATURE_STATUSES if feature.public_beta)
