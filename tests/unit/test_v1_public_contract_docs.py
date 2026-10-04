from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE_DOCS = ROOT / "docs-site" / "src" / "content" / "docs"
CONTRACT_ROOT = SITE_DOCS / "reference" / "contracts"
RUNBOOK_ROOT = SITE_DOCS / "operations" / "runbooks"
DOCS_ROOT = SITE_DOCS / "reference"
MCP_TOOL_SCHEMA = ROOT / "docs-site" / "public" / "contracts" / "mcp-tools-v1.json"

CONTRACT_FILES = (
    "index.md",
    "compatibility-policy.md",
    "mcp-v1.md",
    "http-proxy-v1.md",
    "postgresql-v1.md",
    "mysql-connector-v1.md",
    "configuration-v1.md",
    "helm-v1.md",
    "migrations-v1.md",
    "errors-v1.md",
    "release-candidate.md",
)

RUNBOOK_FILES = (
    "index.md",
    "service-operations.md",
    "availability-rpo-rto.md",
    "backup-restore.md",
    "upgrade-rollback.md",
    "incident-response.md",
    "audit-retention.md",
)


def _route(path: Path) -> str:
    """The site route a page is served at."""
    rel = path.relative_to(SITE_DOCS).with_suffix("")
    slug = str(rel).removesuffix("/index").removesuffix("index")
    return "/" + slug.strip("/") + "/"


def _frontmatter(path: Path) -> dict[str, str]:
    text = _read(path)
    assert text.startswith("---\n"), path
    block = text.split("---\n", 2)[1]
    return {
        key.strip(): value.strip().strip('"')
        for key, _, value in (line.partition(":") for line in block.splitlines())
        if value.strip()
    }


STABLE_MCP_TOOLS = {
    "interlock_query",
    "interlock_describe_access",
    "interlock_list_sources",
    "interlock_describe_source",
    "interlock_discover",
    "interlock_related_documents",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(path: Path) -> str:
    return " ".join(_read(path).split())


def test_v1_contract_package_is_complete_and_interlock_branded() -> None:
    for filename in CONTRACT_FILES:
        path = CONTRACT_ROOT / filename
        assert path.is_file(), path
        meta = _frontmatter(path)
        assert meta.get("normative") == "true", path
        assert "InterLock" in _read(path), path
        # Contract docs must carry the current product name, never a
        # superseded one. (Do not "fix" these by sweeping the literals -
        # they exist precisely to catch a stale brand in a title.)
        assert not meta["title"].startswith("AgentGate"), path
        assert not meta["title"].startswith("Onyx"), path
    assert MCP_TOOL_SCHEMA.is_file()

    for filename in RUNBOOK_FILES:
        path = RUNBOOK_ROOT / filename
        assert path.is_file(), path
        assert _frontmatter(path).get("normative") == "true", path

    index_path = CONTRACT_ROOT / "v1-compatibility-index.md"
    index = _read(index_path)
    assert _frontmatter(index_path)["title"] == "InterLock V1 Public Compatibility Index"
    for filename in CONTRACT_FILES:
        assert f"]({_route(CONTRACT_ROOT / filename)})" in index, filename
    assert "](/contracts/mcp-tools-v1.json)" in index
    for filename in RUNBOOK_FILES:
        assert f"]({_route(RUNBOOK_ROOT / filename)})" in index, filename


def test_contract_markdown_links_resolve_locally() -> None:
    """Every site link in a contract or runbook names a page or file that exists.

    The site build validates links too; this keeps the check in the unit suite.
    """
    markdown_files = [
        *(CONTRACT_ROOT / filename for filename in CONTRACT_FILES),
        *(RUNBOOK_ROOT / filename for filename in RUNBOOK_FILES),
    ]
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    public = ROOT / "docs-site" / "public"

    for path in markdown_files:
        for target in link_pattern.findall(_read(path)):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            route = target.split("#", 1)[0]
            assert route.startswith("/"), (path, target)
            slug = route.strip("/")
            candidates = [
                SITE_DOCS / f"{slug}.md",
                SITE_DOCS / f"{slug}.mdx",
                SITE_DOCS / slug / "index.md",
                public / slug,
            ]
            assert any(c.exists() for c in candidates), (path, target)


def test_mcp_machine_schema_publishes_exact_v1_tool_names() -> None:
    payload = json.loads(_read(MCP_TOOL_SCHEMA))

    assert payload["contractVersion"] == "v1"
    assert payload["$schema"].endswith("draft/2020-12/schema")
    tools = payload["tools"]
    assert {tool["name"] for tool in tools} == STABLE_MCP_TOOLS
    assert len(tools) == len(STABLE_MCP_TOOLS)

    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert set(schema.get("required", ())).issubset(schema["properties"])

    discover = next(tool for tool in tools if tool["name"] == "interlock_discover")
    assert discover["inputSchema"]["properties"]["limit"]["maximum"] == 1000


def test_the_published_tool_schemas_match_the_tools_the_gateway_advertises() -> None:
    """One truth, two files, so pin them to each other.

    `mcp-tools-v1.json` and `MCP_TOOLS` were checked independently against
    hardcoded lists, which means a tool could be added to the gateway and left
    out of the published contract - or the reverse - with every test green.
    """
    from interlock.gateway.mcp_adapter import MCP_TOOLS

    published = {tool["name"]: tool for tool in json.loads(_read(MCP_TOOL_SCHEMA))["tools"]}
    advertised = {
        tool["name"]: tool for tool in MCP_TOOLS if str(tool["name"]).startswith("interlock_")
    }

    assert set(published) == set(advertised)

    for name, tool in published.items():
        published_args = set(tool["inputSchema"].get("properties", {}))
        advertised_args = set(advertised[name]["inputSchema"].get("properties", {}))
        assert published_args == advertised_args, name
        assert set(tool["inputSchema"].get("required", ())) == set(
            advertised[name]["inputSchema"].get("required", ())
        ), name


def test_mcp_transport_and_deprecation_contract_are_explicit() -> None:
    mcp = _read(CONTRACT_ROOT / "mcp-v1.md")
    compatibility = _read(CONTRACT_ROOT / "compatibility-policy.md")

    for phrase in (
        "POST /mcp",
        "JSON-RPC 2.0",
        "Streamable HTTP",
        "Content-Type: application/json",
        "MCP-Protocol-Version",
        "Origin",
        "text/event-stream",
        "Authorization: Bearer",
        "stateless",
        "`unavailable`",
        "`not_found`",
    ):
        assert phrase in mcp
    for tool in STABLE_MCP_TOOLS:
        assert f"`{tool}`" in mcp

    assert "agentgate_*" in compatibility
    assert "remain available through `1.1.x`" in compatibility
    assert "removed in `1.2.0`" in compatibility

    # Phase 0A: the endpoint serves a second, handshake-era dialect so
    # mainstream MCP clients can connect at all. Pin the parts a client
    # integrator depends on.
    assert "initialize" in mcp
    assert "2025-11-25" in mcp
    assert "Mcp-Session-Id" in mcp or "session ID" in mcp
    assert "structuredContent" in mcp


def test_protocol_contracts_distinguish_supported_and_unsupported_behavior() -> None:
    http = _normalized(CONTRACT_ROOT / "http-proxy-v1.md")
    pg = _normalized(CONTRACT_ROOT / "postgresql-v1.md")
    mysql = _normalized(CONTRACT_ROOT / "mysql-connector-v1.md")

    for phrase in (
        "GET|POST|PUT|PATCH|DELETE",
        "Redirects are not followed",
        "Default request allowlist",
        "Default response allowlist",
        "approval-gated",
        "governance scope",
        "bounded",
    ):
        assert phrase in http

    for phrase in (
        "PostgreSQL protocol 3.0",
        "Simple Query",
        "Extended Query",
        "Flush (`H`)",
        "Close (`C`)",
        "CancelRequest",
        "GSS encryption",
        "COPY",
        "`0A000`",
    ):
        assert phrase in pg

    assert "does not provide a MySQL wire-protocol listener" in mysql
    assert "exactly one read-only statement" in mysql
    assert "SELECT ... INTO OUTFILE" in mysql
    assert "`writer`" in mysql
    assert "`schema_owner`" in mysql


def test_config_helm_migration_and_error_contracts_capture_v1_invariants() -> None:
    config = _normalized(CONTRACT_ROOT / "configuration-v1.md")
    helm = _normalized(CONTRACT_ROOT / "helm-v1.md")
    migrations = _normalized(CONTRACT_ROOT / "migrations-v1.md")
    errors = _normalized(CONTRACT_ROOT / "errors-v1.md")

    for phrase in (
        "INTERLOCK_CONFIG_PATH",
        "`INTERLOCK_` prefix",
        "Unknown top-level and nested keys are rejected",
        "database.ssl_mode",
        "verify-full",
        "audit.durability_mode",
        "strict",
    ):
        assert phrase in config

    for phrase in (
        "Gateway StatefulSet",
        "Pre-install/pre-upgrade migration Job",
        "image.digest",
        "auditSpool",
        "same image digest",
        "`/ready`",
    ):
        assert phrase in helm

    for phrase in (
        "schema_migrations",
        "PostgreSQL advisory lock",
        "checksummed with SHA-256",
        "forward-only",
        "immutable",
        "restoring the control database",
    ):
        assert phrase in migrations

    for phrase in (
        "HTTP status",
        "JSON-RPC error",
        "PostgreSQL SQLSTATE",
        "egress_blocked",
        "cache_consistency_unavailable",
        "must not automatically replay a write",
        "`status: unavailable`",
        "FATAL `08004` with the reason",
        "`08006`",
    ):
        assert phrase in errors

    # Approval notifications ship credentials only as secret references, and
    # the approval expiry is no longer hard-coded. Both are operator-visible.
    assert "notifications.slack_bot_token_ref" in config
    assert "approvals.expiry_seconds" in config
    assert "MUST be secret references" in config


def test_operations_package_defines_recovery_and_compliance_targets() -> None:
    availability = _read(RUNBOOK_ROOT / "availability-rpo-rto.md")
    backup = _read(RUNBOOK_ROOT / "backup-restore.md")
    upgrade = _read(RUNBOOK_ROOT / "upgrade-rollback.md")
    incident = _read(RUNBOOK_ROOT / "incident-response.md")
    retention = _read(RUNBOOK_ROOT / "audit-retention.md")

    assert "Reference RPO" in availability
    assert "Reference RTO" in availability
    assert "Control PostgreSQL" in availability
    assert "Audit database and durable spool" in availability
    assert "Quarterly Recovery Exercise" in availability

    for phrase in (
        "PostgreSQL WAL",
        "audit spool",
        "Isolated Restore Procedure",
        "Recovery Validation",
    ):
        assert phrase in backup

    assert "Application-Only Rollback" in upgrade
    assert "Database Restore Rollback" in upgrade
    assert "V1 has no automatic down-migration contract" in upgrade
    assert "SEV-0" in incident and "SEV-3" in incident
    assert "90 days online/queryable" in retention
    assert "365 days encrypted archive" in retention
    assert "no UPDATE or DELETE" in retention


def test_connector_matrix_is_truthful_about_stable_beta_and_deferred() -> None:
    matrix = _read(DOCS_ROOT / "connector-support-matrix.md")

    meta = _frontmatter(DOCS_ROOT / "connector-support-matrix.md")
    assert meta["title"] == "Connector support matrix"
    assert meta.get("normative") == "true"
    assert "Stable V1" in matrix
    assert "Beta V1" in matrix
    assert "Deferred" in matrix
    assert "no MySQL wire listener" in matrix
    assert "Unsupported writes must fail closed" in matrix

    for connector in (
        "PostgreSQL",
        "MySQL/MariaDB",
        "HTTP",
        "Amazon S3",
        "DigitalOcean Spaces",
        "Slack",
        "GitHub",
        "GitLab",
        "Snowflake",
        "Zendesk",
        "OpenSearch",
        "Elasticsearch",
        "Qdrant",
        "Salesforce",
        "Notion",
        "Google Workspace",
    ):
        assert connector in matrix


def test_public_v1_docs_contain_no_live_credentials_or_private_identifiers() -> None:
    paths = [
        *(CONTRACT_ROOT / filename for filename in CONTRACT_FILES),
        *(RUNBOOK_ROOT / filename for filename in RUNBOOK_FILES),
        DOCS_ROOT / "connector-support-matrix.md",
        CONTRACT_ROOT / "v1-compatibility-index.md",
    ]
    combined = "\n".join(_read(path) for path in paths)

    forbidden_patterns = {
        "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
        "GitHub token": r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b",
        "Slack token": r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
        "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "email address": r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "local absolute path": r"/(?:Users|home)/[^\s)`]+",
    }
    for label, pattern in forbidden_patterns.items():
        assert re.search(pattern, combined, flags=re.IGNORECASE) is None, label

    for private_example in (
        "ContextData/",
        "contextdata.ai",
        "actualize_",
        "claims-test",
    ):
        assert private_example not in combined


def test_release_candidate_path_requires_one_commit_and_one_digest() -> None:
    release = _normalized(CONTRACT_ROOT / "release-candidate.md")

    assert "`1.0.0-rc.1`" in release
    assert "One source commit produces" in release
    assert "same image digest" in release
    assert "Disposable DigitalOcean (DOKS) deployment" in release
    assert "AWS (EKS) deployment is required before GA" in release
    assert "No open P0 finding" in release
    assert "Do not mutate or silently replace a published candidate artifact" in release


def test_a_client_is_listed_as_certified_only_when_something_drives_it() -> None:
    """The PostgreSQL contract may not certify a client the suite never uses.

    Phase 8 of the governance audit. The Client Compatibility Target section
    previously read "must certify psql, Psycopg 3, asyncpg, PostgreSQL JDBC,
    and pgx/Go" - an intention stated in the grammar of a completed
    obligation. Only asyncpg drives the V1 listener in any test, so four of
    the five named clients had no evidence at all, and a reader had no way to
    tell which.

    This checks the direction that matters: a client named as certified must
    appear in the test suite. The reverse would be a nice-to-have; this is the
    one that keeps the document from overstating what runs.
    """
    contract = (CONTRACT_ROOT / "postgresql-v1.md").read_text()
    certified_line = next(
        line for line in contract.splitlines() if line.startswith("Certified today:")
    )

    suite = " ".join(
        path.read_text()
        for path in (ROOT / "tests").rglob("*.py")
        if "e2e" in path.parts or "live" in path.parts
    )

    aliases = {
        "asyncpg": ("asyncpg",),
        "Psycopg 3": ("psycopg",),
        "psql": ("psql",),
        "PostgreSQL JDBC": ("jdbc",),
        "pgx/Go": ("pgx",),
    }
    for client, needles in aliases.items():
        if client not in certified_line:
            continue
        assert any(needle in suite.lower() for needle in needles), (
            f"postgresql-v1.md lists {client} as certified, but no e2e or live "
            "test references it. Certification is a dated report tied to the "
            "release commit, not a name in a list."
        )
