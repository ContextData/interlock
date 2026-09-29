from __future__ import annotations

import socket

import pytest

from interlock.connections.connectors import (
    CONNECTOR_DEFINITIONS,
    connector_key_for,
    get_adapter,
    get_connector,
    role_templates_for_connector,
    sanitize_config,
    source_metadata_for_connector,
)
from interlock.connections.source_config import (
    SourceConfigValidationError,
    validate_source_config,
)
from interlock.core.source_roles import SourceRoleEvaluator, build_permission_request

UNSUPPORTED_WRITE_ACTION_PREFIXES = (
    "db.table.insert",
    "db.table.update",
    "db.table.delete_rows",
    "db.schema.create_table",
    "db.schema.alter_table",
    "db.schema.drop_table",
    "db.execute_raw",
    "storage.object.write",
    "storage.object.delete",
    "saas.object.write",
    "saas.object.delete",
    "search.document.write",
    "search.document.delete",
    "repo.action.execute",
)


class FakePool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


class FakeProbeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeHTTPProbeClient:
    status_code = 200

    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "FakeHTTPProbeClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, *_args, **_kwargs) -> FakeProbeResponse:
        return FakeProbeResponse(self.status_code)


@pytest.fixture(autouse=True)
def _safe_test_dns(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "api.example.test":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", port or 443),
                )
            ]
        return []

    monkeypatch.setattr("interlock.security.egress.socket.getaddrinfo", fake_getaddrinfo)


def test_tier_one_connectors_are_registered() -> None:
    for key in ("mysql", "mariadb", "s3", "snowflake", "zendesk", "slack", "github", "gitlab"):
        assert key in CONNECTOR_DEFINITIONS
        assert CONNECTOR_DEFINITIONS[key].tier == 1
        assert CONNECTOR_DEFINITIONS[key].status == "native"


def test_tier_two_connectors_are_registered() -> None:
    for key in (
        "opensearch",
        "elasticsearch",
        "qdrant",
        "mongodb",
        "salesforce",
        "atlassian",
        "google_workspace",
        "bigquery",
        "servicenow",
        "mssql",
        "notion",
    ):
        assert key in CONNECTOR_DEFINITIONS
        assert CONNECTOR_DEFINITIONS[key].tier == 2


def test_new_priority_connectors_are_native_or_compatible() -> None:
    expected = {
        "digitalocean_spaces": ("DigitalOcean Spaces", "object_storage", "native"),
        "opensearch": ("OpenSearch", "search", "native"),
        "elasticsearch": ("Elasticsearch", "search", "native"),
        "qdrant": ("Qdrant", "search", "native"),
        "salesforce": ("Salesforce", "saas", "native"),
        "notion": ("Notion", "saas", "native"),
    }
    for key, (name, family, status) in expected.items():
        definition = CONNECTOR_DEFINITIONS[key]
        assert name in definition.name
        assert definition.family == family
        assert definition.status == status
        assert definition.capabilities.supports_discovery is True
        assert definition.capabilities.supports_ingestion is True


@pytest.mark.asyncio
async def test_connector_capabilities_do_not_advertise_unimplemented_writes() -> None:
    for key in (
        "zendesk",
        "github",
        "gitlab",
        "opensearch",
        "elasticsearch",
        "qdrant",
        "salesforce",
        "notion",
    ):
        definition = CONNECTOR_DEFINITIONS[key]
        adapter = get_adapter(key, {"connector_key": key})
        assert definition.capabilities.supports_write is False
        with pytest.raises(NotImplementedError):
            await adapter.execute_write({})


def test_unsupported_write_connectors_do_not_publish_write_templates() -> None:
    for definition in CONNECTOR_DEFINITIONS.values():
        if definition.capabilities.supports_write:
            continue
        for role_key, permissions in definition.role_templates.items():
            for permission in permissions:
                action = str(permission.get("action") or "")
                assert not action.startswith(UNSUPPORTED_WRITE_ACTION_PREFIXES), (
                    f"{definition.key}.{role_key} publishes write action {action!r} "
                    "while supports_write is false"
                )


def test_incremental_sync_capability_requires_stateful_delta_support() -> None:
    stateful_sync_connectors = {"s3", "digitalocean_spaces", "zendesk"}
    for key in (
        "slack",
        "github",
        "gitlab",
        "opensearch",
        "elasticsearch",
        "qdrant",
        "salesforce",
        "notion",
        "atlassian",
        "google_workspace",
        "servicenow",
    ):
        assert CONNECTOR_DEFINITIONS[key].capabilities.supports_incremental_sync is False
    for key in stateful_sync_connectors:
        assert CONNECTOR_DEFINITIONS[key].capabilities.supports_incremental_sync is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
async def test_generic_http_probe_treats_auth_rate_limit_and_server_errors_as_unhealthy(
    monkeypatch, status_code: int
) -> None:
    FakeHTTPProbeClient.status_code = status_code
    monkeypatch.setattr("httpx.AsyncClient", FakeHTTPProbeClient)
    adapter = get_adapter("http", {})

    result = await adapter.probe({"base_url": "https://api.example.test"})

    assert result.healthy is False
    # The message names what was probed, so a 404 at an API root reads as
    # "this route", not "this source is unreachable".
    assert (result.error or "").startswith(f"HTTP {status_code} from /")


def test_connector_key_compatibility_mapping() -> None:
    assert connector_key_for("http", {}) == "generic_rest"
    assert connector_key_for("saas", {"connector_key": "slack"}) == "slack"
    assert get_connector("warehouse", {"connector_key": "snowflake"}).key == "snowflake"
    assert (
        get_connector("object_storage", {"connector_key": "digitalocean_spaces"}).key
        == "digitalocean_spaces"
    )
    assert get_connector("search", {"connector_key": "qdrant"}).key == "qdrant"


def test_source_metadata_records_connector_contract() -> None:
    metadata = source_metadata_for_connector("s3")
    assert metadata["connector_key"] == "s3"
    assert metadata["connector_family"] == "object_storage"
    assert metadata["source_roles_version"] == 2
    assert metadata["connector_capabilities"]["supports_ingestion"] is True


def test_sanitize_config_masks_literals_and_preserves_refs() -> None:
    definition = get_connector("s3")
    safe = sanitize_config(
        {
            "bucket": "docs",
            "aws_access_key_id": "AKIA",
            "aws_secret_access_key": "secret",
            "aws_secret_access_key_ref": "env://AWS_SECRET_ACCESS_KEY",
        },
        definition,
    )
    assert safe["bucket"] == "docs"
    assert safe["aws_access_key_id"] == "<configured>"
    assert safe["aws_secret_access_key"] == "<configured>"
    assert safe["aws_secret_access_key_ref"] == "env:AWS_SECRET_ACCESS_KEY"


def test_sanitize_config_masks_nested_secret_like_fields_and_dsns() -> None:
    safe = sanitize_config(
        {
            "base_url": "https://api.example.test",
            "connection_string": "postgres://user:pass@db.example.test:5432/app",
            "headers": {
                "Authorization": "Bearer secret",
                "X-Api-Key": "secret-key",
                "Accept": "application/json",
            },
            "oauth": {
                "access_token": "token",
                "refreshToken": "refresh",
                "session_cookie": "cookie",
            },
            "service_account_json": {"private_key": "-----BEGIN PRIVATE KEY-----"},
            "safe_refs": ["env://TOKEN_REF"],
        }
    )

    assert safe["base_url"] == "https://api.example.test"
    assert safe["connection_string"] == "<configured>"
    assert safe["headers"]["Authorization"] == "<configured>"
    assert safe["headers"]["X-Api-Key"] == "<configured>"
    assert safe["headers"]["Accept"] == "application/json"
    assert safe["oauth"]["access_token"] == "<configured>"
    assert safe["oauth"]["refreshToken"] == "<configured>"
    assert safe["oauth"]["session_cookie"] == "<configured>"
    assert safe["service_account_json"] == "<configured>"
    assert safe["safe_refs"] == ["env://TOKEN_REF"]


def test_sanitize_config_redacts_uri_userinfo_in_non_secret_fields() -> None:
    safe = sanitize_config({"endpoint_url": "https://user:pass@example.test/api"})

    assert safe["endpoint_url"] == "https://<redacted>@example.test/api"


def test_role_templates_use_connector_native_actions() -> None:
    s3_roles = role_templates_for_connector("s3")
    assert any(p["action"] == "storage.object.read" for p in s3_roles["reader"])
    assert any(p["action"] == "discovery.search" for p in s3_roles["reader"])

    snowflake_roles = role_templates_for_connector("snowflake")
    assert any(p["action"] == "db.table.select" for p in snowflake_roles["analyst"])
    assert any(p["action"] == "discovery.search" for p in snowflake_roles["analyst"])

    slack_roles = role_templates_for_connector("slack")
    assert any(p["action"] == "collab.message.read" for p in slack_roles["channel_reader"])
    assert any(p["action"] == "collab.file.read" for p in slack_roles["compliance_reader"])

    repo_roles = role_templates_for_connector("github")
    assert any(p["action"] == "repo.code.read" for p in repo_roles["code_reader"])
    assert "triage_operator" not in repo_roles

    gitlab_roles = role_templates_for_connector("gitlab")
    assert any(p["action"] == "repo.code.read" for p in gitlab_roles["code_reader"])
    assert "ci_operator" not in gitlab_roles

    zendesk_roles = role_templates_for_connector("zendesk")
    assert any(
        p["action"] == "saas.object.read" and p["resource_pattern"] == "zendesk:ticket/*"
        for p in zendesk_roles["support_reader"]
    )
    assert "support_operator" not in zendesk_roles

    search_roles = role_templates_for_connector("opensearch")
    assert any(p["action"] == "search.query" for p in search_roles["query_reader"])
    assert "index_admin" not in search_roles

    qdrant_roles = role_templates_for_connector("qdrant")
    assert any(p["resource_type"] == "search.collection" for p in qdrant_roles["collection_reader"])
    assert "point_writer" not in qdrant_roles

    notion_roles = role_templates_for_connector("notion")
    assert any(
        p["action"] == "saas.object.search" and p["resource_pattern"] == "notion:*/*"
        for p in notion_roles["workspace_reader"]
    )


def test_tier_one_adapters_are_native_implementations() -> None:
    expected = {
        "mysql": "MySQLAdapter",
        "mariadb": "MySQLAdapter",
        "s3": "S3ConnectorAdapter",
        "snowflake": "SnowflakeAdapter",
        "zendesk": "ZendeskAdapter",
        "slack": "SlackAdapter",
        "github": "GitHubAdapter",
        "gitlab": "GitLabAdapter",
    }
    for key, class_name in expected.items():
        assert get_adapter(key, {"connector_key": key}).__class__.__name__ == class_name


def test_new_priority_adapters_are_native_implementations() -> None:
    expected = {
        "digitalocean_spaces": "DigitalOceanSpacesAdapter",
        "opensearch": "OpenSearchAdapter",
        "elasticsearch": "OpenSearchAdapter",
        "qdrant": "QdrantSourceAdapter",
        "salesforce": "SalesforceAdapter",
        "notion": "NotionAdapter",
    }
    for key, class_name in expected.items():
        assert get_adapter(key, {"connector_key": key}).__class__.__name__ == class_name


def test_mysql_permission_request_extracts_sql_action() -> None:
    adapter = get_adapter("mysql", {"connector_key": "mysql"})
    request = adapter.build_permission_request(
        source_id="mysql_orders",
        identity_id=10,
        operation="SELECT id, email FROM customers",
        metadata={"query": "SELECT id, email FROM customers"},
    )
    assert request.action == "db.table.select"
    assert request.risk_level == "low"


def test_generic_database_connector_does_not_fall_back_to_http_actions() -> None:
    adapter = get_adapter("postgresql", {"connector_key": "postgresql"})

    read_request = adapter.build_permission_request(
        source_id="pg_source",
        identity_id=10,
        operation="read",
    )
    write_request = adapter.build_permission_request(
        source_id="pg_source",
        identity_id=10,
        operation="write",
    )

    assert read_request.protocol == "database"
    assert read_request.action == "db.table.select"
    assert read_request.resource_type == "db.table"
    assert write_request.protocol == "database"
    assert write_request.action == "db.execute_raw"
    assert write_request.resource_type == "db.raw"


@pytest.mark.asyncio
async def test_mysql_execute_read_rejects_write_sql_before_connect() -> None:
    adapter = get_adapter("mysql", {"connector_key": "mysql"})

    with pytest.raises(PermissionError, match="only accepts read SQL"):
        await adapter.execute_read(
            {
                "source_id": "mysql_orders",
                "query": "DELETE FROM customers WHERE id = 1",
                "connection_config": {},
            }
        )


@pytest.mark.asyncio
async def test_mysql_execute_read_rejects_multistatement_write_before_connect() -> None:
    adapter = get_adapter("mysql", {"connector_key": "mysql"})

    with pytest.raises(PermissionError, match="single read SQL"):
        await adapter.execute_read(
            {
                "source_id": "mysql_orders",
                "query": "SELECT name FROM customers; DELETE FROM customers WHERE id = 1",
                "connection_config": {},
            }
        )


def test_snowflake_permission_request_extracts_qualified_table() -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    request = adapter.build_permission_request(
        source_id="sf_analytics",
        identity_id=10,
        operation="SELECT ID FROM ANALYTICS.PUBLIC.CUSTOMERS",
        metadata={"query": "SELECT ID FROM ANALYTICS.PUBLIC.CUSTOMERS"},
    )
    assert request.action == "db.table.select"
    assert request.resources == ["ANALYTICS.PUBLIC.CUSTOMERS"]
    assert request.schemas == ["ANALYTICS.PUBLIC"]


def test_s3_permission_request_uses_object_storage_actions() -> None:
    adapter = get_adapter("s3", {"connector_key": "s3"})
    request = adapter.build_permission_request(
        source_id="s3_docs",
        identity_id=10,
        operation="delete",
        metadata={"asset_ref": "s3://docs/reports/q1.pdf"},
    )
    assert request.action == "storage.object.delete"
    assert request.resources == ["s3://docs/reports/q1.pdf"]
    assert request.metadata["file_extension"] == ".pdf"


def test_s3_permission_request_builds_prefix_resource() -> None:
    adapter = get_adapter("s3", {"connector_key": "s3"})
    request = adapter.build_permission_request(
        source_id="s3_docs",
        identity_id=10,
        operation="list",
        metadata={"bucket": "docs", "prefix": "reports/"},
    )
    assert request.action == "storage.prefix.list"
    assert request.resources == ["s3://docs/reports/*"]


def test_digitalocean_spaces_permission_request_uses_storage_actions() -> None:
    adapter = get_adapter("digitalocean_spaces", {"connector_key": "digitalocean_spaces"})
    request = adapter.build_permission_request(
        source_id="spaces_claims",
        identity_id=10,
        operation="read",
        metadata={"asset_ref": "s3://example-claims-bucket/org/report.pdf"},
    )
    assert request.action == "storage.object.read"
    assert request.resources == ["s3://example-claims-bucket/org/report.pdf"]
    assert request.metadata["connector_key"] == "digitalocean_spaces"


def test_object_storage_permission_request_preserves_s3_uri_resource() -> None:
    request = build_permission_request(
        protocol="object_storage",
        source_id="s3_docs",
        identity_id=10,
        operation="write",
        path="s3://docs/reports/cert.txt",
    )

    assert request.action == "storage.object.write"
    assert request.resources == ["s3://docs/reports/cert.txt"]


def test_repo_permission_request_maps_code_reads() -> None:
    adapter = get_adapter("github", {"connector_key": "github"})
    request = adapter.build_permission_request(
        source_id="gh",
        identity_id=10,
        operation="code",
        metadata={"asset_ref": "github://file/acme/app/README.md"},
    )
    assert request.action == "repo.code.read"
    assert request.resources == ["github://file/acme/app/README.md"]


def test_search_permission_requests_are_index_and_document_scoped() -> None:
    adapter = get_adapter("opensearch", {"connector_key": "opensearch"})

    query = adapter.build_permission_request(
        source_id="os",
        identity_id=10,
        operation="query",
        metadata={"index": "claims", "query": {"match_all": {}}},
    )
    document = adapter.build_permission_request(
        source_id="os",
        identity_id=10,
        operation="document",
        metadata={"asset_ref": "opensearch://claims/abc123"},
    )

    assert query.action == "search.query"
    assert query.resources == ["claims"]
    assert document.action == "search.document.read"
    assert document.resources == ["opensearch://claims/abc123"]


def test_qdrant_permission_request_is_collection_scoped() -> None:
    adapter = get_adapter("qdrant", {"connector_key": "qdrant"})
    request = adapter.build_permission_request(
        source_id="qd",
        identity_id=10,
        operation="point",
        metadata={"asset_ref": "qdrant://claims/point/42"},
    )
    assert request.action == "search.document.read"
    assert request.resources == ["qdrant://claims/point/42"]
    assert request.metadata["collection"] == "claims"
    assert request.resource_type == "search.document"


def test_qdrant_collection_reader_template_allows_its_own_query() -> None:
    """The template grants search.query on search.collection; the request must
    carry that resource type or the evaluator compares it with search.index."""
    adapter = get_adapter("qdrant", {"connector_key": "qdrant"})
    request = adapter.build_permission_request(
        source_id="qd", identity_id=10, operation="query", metadata={"collection": "claims"}
    )
    assert request.resource_type == "search.collection"
    template = CONNECTOR_DEFINITIONS["qdrant"].role_templates["collection_reader"]
    statement = next(p for p in template if p["action"] == request.action)
    assert statement["resource_type"] == "search.collection"


def test_saas_permission_requests_for_salesforce_and_notion_are_scoped() -> None:
    salesforce = get_adapter("salesforce", {"connector_key": "salesforce"})
    notion = get_adapter("notion", {"connector_key": "notion"})

    sf_request = salesforce.build_permission_request(
        source_id="sf",
        identity_id=10,
        operation="read",
        metadata={"object_type": "Account", "object_id": "001xx"},
    )
    notion_request = notion.build_permission_request(
        source_id="notion",
        identity_id=10,
        operation="read",
        metadata={"asset_ref": "notion://page/page-1"},
    )

    assert sf_request.action == "saas.object.read"
    assert sf_request.resources == ["salesforce:Account/001xx"]
    assert notion_request.action == "saas.object.read"
    assert notion_request.resources == ["notion:page/page-1"]


@pytest.mark.asyncio
async def test_source_role_evaluator_supports_storage_actions() -> None:
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                {
                    "permission_id": 1,
                    "role_id": 1,
                    "role_key": "reader",
                    "effect": "allow",
                    "action": "storage.object.read",
                    "resource_type": "storage.object",
                    "resource_pattern": "/reports/*",
                    "constraints": {},
                }
            ]
        )
    )
    request = build_permission_request(
        protocol="object_storage",
        source_id="s3_docs",
        identity_id=10,
        operation="read",
        path="/reports/q1.pdf",
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_source_role_evaluator_supports_storage_file_extension_constraints() -> None:
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                {
                    "permission_id": 1,
                    "role_id": 1,
                    "role_key": "markdown_reader",
                    "effect": "allow",
                    "action": "storage.object.read",
                    "resource_type": "storage.object",
                    "resource_pattern": "s3://docs/discovery/*",
                    "constraints": {"file_extensions": [".md"]},
                }
            ]
        )
    )
    request = get_adapter("s3", {"connector_key": "s3"}).build_permission_request(
        source_id="s3_docs",
        identity_id=10,
        operation="read",
        metadata={"asset_ref": "s3://docs/discovery/runbook.md"},
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_source_role_evaluator_supports_search_constraints() -> None:
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                {
                    "permission_id": 1,
                    "role_id": 1,
                    "role_key": "claims_reader",
                    "effect": "allow",
                    "action": "search.query",
                    "resource_type": "search.index",
                    "resource_pattern": "claims-*",
                    "constraints": {"indices": ["claims-2026"]},
                }
            ]
        )
    )
    request = get_adapter("opensearch", {"connector_key": "opensearch"}).build_permission_request(
        source_id="os",
        identity_id=10,
        operation="query",
        metadata={"index": "claims-2026"},
    )

    decision = await evaluator.evaluate(request)

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_metadata_resource_type_cannot_broaden_permission() -> None:
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                {
                    "permission_id": 1,
                    "role_id": 1,
                    "role_key": "schema_owner",
                    "effect": "allow",
                    "action": "db.schema.drop_table",
                    "resource_type": "db.schema",
                    "resource_pattern": "*",
                    "constraints": {},
                }
            ]
        )
    )
    request = build_permission_request(
        protocol="postgresql",
        source_id="pg",
        identity_id=10,
        operation="SELECT",
        tables=["customers"],
        metadata={"resource_type": "db.schema"},
    )

    assert request.action == "db.table.select"
    assert request.resource_type == "db.table"
    decision = await evaluator.evaluate(request)
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_s3_execute_write_puts_and_deletes_objects(monkeypatch) -> None:
    # Patches a real aiobotocore attribute, so it only runs when the optional
    # connectors-tier1 extra is installed.
    pytest.importorskip("aiobotocore", reason="requires the connectors-tier1 extra")

    calls: list[tuple[str, dict[str, object]]] = []

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def put_object(self, **kwargs: object) -> dict[str, str]:
            calls.append(("put", kwargs))
            return {"ETag": '"etag"'}

        async def delete_object(self, **kwargs: object) -> dict[str, str]:
            calls.append(("delete", kwargs))
            return {"VersionId": "v1"}

    class FakeSession:
        def create_client(self, *_args: object, **_kwargs: object) -> FakeClient:
            return FakeClient()

    monkeypatch.setattr("aiobotocore.session.get_session", lambda: FakeSession())
    adapter = get_adapter("s3", {"connector_key": "s3"})
    config = {"bucket": "docs", "region_name": "us-east-1"}

    put_result = await adapter.execute_write(
        {
            "operation": "write",
            "connection_config": config,
            "asset_ref": "s3://docs/reports/cert.txt",
            "body": "cert",
        }
    )
    delete_result = await adapter.execute_write(
        {
            "operation": "delete",
            "connection_config": config,
            "asset_ref": "s3://docs/reports/cert.txt",
        }
    )

    assert put_result == {"asset_ref": "s3://docs/reports/cert.txt", "etag": '"etag"'}
    assert delete_result == {
        "asset_ref": "s3://docs/reports/cert.txt",
        "deleted": True,
        "version_id": "v1",
    }
    assert calls == [
        ("put", {"Bucket": "docs", "Key": "reports/cert.txt", "Body": b"cert"}),
        ("delete", {"Bucket": "docs", "Key": "reports/cert.txt"}),
    ]


@pytest.mark.asyncio
async def test_saas_presence_probe_requires_connector_specific_fields() -> None:
    adapter = get_adapter("slack", {"connector_key": "slack"})
    result = await adapter.probe({})
    assert result.healthy is False
    assert "workspace" in (result.error or "")


def test_database_connectors_declare_the_database_field() -> None:
    """A database connector must permit the key its adapter reads.

    `postgresql` shipped with no `credential_fields` at all, so
    `validate_source_config` fell back to `COMMON_CONFIG_KEYS`, which has no
    `database`. The strict admin path then rejected the one key the connector
    cannot work without, while the lenient probe path accepted it.
    """
    for key, spec in CONNECTOR_DEFINITIONS.items():
        if spec.status == "planned" or spec.family != "database":
            continue
        assert (
            "database" in spec.credential_fields
        ), f"connector {key} is a database connector but does not declare 'database'"
        assert spec.secret_fields, f"connector {key} declares no secret fields"


def test_postgresql_config_survives_strict_admin_validation() -> None:
    """The strict admin create/update path must accept a usable PG config.

    `POST /api/data-sources` passes `strict_unknown=True`, so this is the
    assertion that the API can register a PostgreSQL source at all.
    """
    spec = get_connector("postgresql")
    config = {
        "host": "db.internal",
        "port": 5432,
        "database": "sales",
        "user": "interlock_ro",
        "password_ref": "env://SALES_DB_PASSWORD",
        "sslmode": "require",
        "verify_ssl": True,
        "allow_private_egress": True,
    }

    validated = validate_source_config(
        config,
        connector_key=spec.key,
        source_type=spec.source_type,
        source_id="sales_pg",
        allowed_fields=spec.credential_fields,
        secret_fields=spec.secret_fields,
        strict_unknown=True,
    )

    assert validated["database"] == "sales"
    assert validated["sslmode"] == "require"


def test_postgresql_rejects_executable_config_keys() -> None:
    """Widening the allowlist must not weaken the executable-key guard."""
    spec = get_connector("postgresql")

    with pytest.raises(SourceConfigValidationError):
        validate_source_config(
            {"host": "db.internal", "database": "sales", "command": "/bin/sh"},
            connector_key=spec.key,
            source_type=spec.source_type,
            source_id="sales_pg",
            allowed_fields=spec.credential_fields,
            secret_fields=spec.secret_fields,
            strict_unknown=True,
        )
