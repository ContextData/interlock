"""Dashboard route tests for source-onboarding endpoints.

End-to-end through the admin app with mocked PG/Redis:

  GET  /dashboard/data-sources/new                        -> form partial
  POST /dashboard/data-sources/test  (form-encoded body)  -> result strip
  POST /dashboard/data-sources/create (form-encoded)      -> data sources table
  GET  /dashboard/access-control/identities/new           -> identity form
  POST /dashboard/access-control/identities/create        -> reveal partial
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.config import InterLockConfig


class FakeConn:
    def __init__(self, parent: FakePool) -> None:
        self._parent = parent

    async def execute(self, *args, **kwargs):
        self._parent._calls.append(("execute", args))
        return "OK"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self) -> None:
        self._calls: list = []
        self._existing_source = False
        self._existing_identity = False

    async def fetch(self, *args, **kwargs):
        return []

    async def fetchval(self, query, *args):
        self._calls.append(("fetchval", (query, *args)))
        if "FROM data_sources" in query:
            return 1 if self._existing_source else None
        if "FROM identities" in query:
            return 1 if self._existing_identity else None
        return 0

    async def fetchrow(self, *args, **kwargs):
        if args and "source_type, metadata FROM data_sources" in args[0]:
            return {
                "source_id": "shop",
                "name": "Shop",
                "source_type": "postgresql",
                "metadata": {"connector_key": "postgresql"},
            }
        return None

    async def execute(self, *args, **kwargs):
        self._calls.append(("execute", args))
        return "OK"

    def acquire(self):
        return FakeConn(self)


def _make_app(pool: FakePool):
    config = InterLockConfig()
    redis = MagicMock()
    redis.aclose = AsyncMock()
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as cp,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch("interlock.admin.app.create_redis_client", new_callable=AsyncMock) as cr,
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        cp.return_value = pool
        cr.return_value = redis
        app = create_app(config=config)
    app.state.pg_pool = pool
    app.state.redis = redis
    app.state.auth_disabled = True
    return app


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def app(pool: FakePool):
    return _make_app(pool)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_new_data_source_form_renders(client: AsyncClient) -> None:
    resp = await client.get("/dashboard/data-sources/new")
    assert resp.status_code == 200
    body = resp.text
    assert "New Data Source" in body
    # The source id is generated from the display name, never asked for.
    assert 'name="source_id"' not in body
    assert 'name="name"' in body
    assert "Snowflake" in body
    assert "Snowflake account" in body
    assert 'name="private_key_ref"' in body
    assert 'name="collect_query_history"' in body
    assert "SNOWFLAKE_PASSWORD" in body
    assert "Zendesk brand ID" in body
    assert 'name="include_ticket_comments"' in body
    assert "Repository code paths" in body
    assert 'name="max_prs_per_repo"' in body
    assert 'name="max_pipelines_per_project"' in body
    assert "DigitalOcean Spaces" in body
    assert 'name="indices"' in body
    assert 'name="collections"' in body
    assert 'name="max_points_per_collection"' in body
    assert 'name="instance_url"' in body
    assert 'name="security_token_ref"' in body
    assert 'name="integration_token_ref"' in body
    assert 'name="notion_version"' in body
    assert "Include thread replies" in body
    assert 'name="max_messages_per_channel"' in body
    assert "Source roles" in body
    assert 'name="create_default_roles"' in body
    assert 'hx-post="/dashboard/data-sources/test"' in body


@pytest.mark.anyio
async def test_test_endpoint_returns_failure_for_unreachable_host(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/test",
        data={
            "source_type": "postgresql",
            "host": "127.0.0.1",
            "port": "1",
            "database": "",
            "user": "",
            "password": "",
            "base_url": "",
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert "Connection failed" in body
    assert "health-red" in body


@pytest.mark.anyio
async def test_create_endpoint_persists_new_source(client: AsyncClient, pool: FakePool) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "cache_strategy": "deterministic_first",
            "host": "postgres",
            "port": "5432",
            "database": "shop",
            "user": "",
            "password": "",
            "base_url": "",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200
    # The response is the data sources table partial.
    assert "data-sources-table" in resp.text
    # The pool saw the INSERT + NOTIFY.
    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserted, "INSERT into data_sources was not executed"
    metadata = inserted[-1][1][6]
    assert metadata["source_roles_version"] == 2
    assert metadata["connector_key"] == "postgresql"
    assert metadata["connector_family"] == "database"
    role_inserts = [c for c in pool._calls if "INSERT INTO source_roles" in c[1][0]]
    assert role_inserts, "default source role templates were not created"
    notify = [c for c in pool._calls if "NOTIFY onyx_config_changed" in c[1][0]]
    assert notify, "NOTIFY onyx_config_changed was not fired"


@pytest.mark.anyio
async def test_create_source_role_writes_permission_admin_audit(
    client: AsyncClient,
    pool: FakePool,
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/shop/roles",
        data={
            "role_key": "table_reader",
            "name": "Table Reader",
            "description": "Read customers only",
            "enabled": "on",
            "permission_effect": "allow",
            "permission_action": "db.table.select",
            "permission_resource_type": "db.table",
            "permission_resource_pattern": "public.customers",
            "permission_constraints": '{"risk_levels":["low"]}',
        },
        follow_redirects=False,
    )

    assert resp.status_code in {200, 303}
    audit_calls = [c for c in pool._calls if "INSERT INTO admin_audit_log" in c[1][0]]
    assert audit_calls
    audit_args = audit_calls[-1][1]
    assert audit_args[3] == "source_role.create"
    assert audit_args[4] == "source_role"
    detail = audit_args[6]
    assert detail["after"]["role_key"] == "table_reader"
    assert detail["after"]["permissions"] == [
        {
            "effect": "allow",
            "action": "db.table.select",
            "resource_type": "db.table",
            "resource_pattern": "public.customers",
            "constraints": {"risk_levels": ["low"]},
        }
    ]
    assert detail["changed_fields"] == [
        "description",
        "enabled",
        "name",
        "permissions",
        "review_required",
        "role_key",
        "source_id",
    ]


@pytest.mark.anyio
async def test_create_alert_rule_writes_redacted_admin_audit(
    client: AsyncClient,
    pool: FakePool,
) -> None:
    resp = await client.post(
        "/dashboard/alerts/create",
        data={
            "name": "Error Rate",
            "description": "High error rate",
            "condition_type": "error_rate",
            "comparator": ">",
            "threshold": "0.5",
            "window_seconds": "300",
            "source_id": "shop",
            "notification_channel": "webhook",
            "notification_target": "https://hooks.example.test/path?token=secret-value",
        },
        follow_redirects=False,
    )

    assert resp.status_code in {200, 303}
    audit_calls = [c for c in pool._calls if "INSERT INTO admin_audit_log" in c[1][0]]
    assert audit_calls
    audit_args = audit_calls[-1][1]
    assert audit_args[3] == "alert_rule.create"
    assert audit_args[4] == "alert_rule"
    detail = audit_args[6]
    assert detail["after"]["name"] == "Error Rate"
    assert detail["after"]["source_id"] == "shop"
    assert "secret-value" not in str(detail)
    assert "[REDACTED]" in str(detail)


@pytest.mark.anyio
async def test_toggle_alert_rule_writes_before_after_admin_audit(
    client: AsyncClient,
    pool: FakePool,
) -> None:
    pool.fetchrow = AsyncMock(return_value={"id": 7, "name": "Error Rate", "enabled": True})

    resp = await client.post("/dashboard/alerts/7/toggle", follow_redirects=False)

    assert resp.status_code in {200, 303}
    audit_calls = [c for c in pool._calls if "INSERT INTO admin_audit_log" in c[1][0]]
    assert audit_calls
    audit_args = audit_calls[-1][1]
    assert audit_args[3] == "alert_rule.toggle"
    assert audit_args[4] == "alert_rule"
    detail = audit_args[6]
    assert detail["before"]["enabled"] is True
    assert detail["after"]["enabled"] is False
    assert detail["changed_fields"] == ["enabled"]


@pytest.mark.anyio
async def test_create_snowflake_source_persists_connector_config(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "sf_analytics",
            "name": "Snowflake Analytics",
            "source_type": "warehouse",
            "connector_key": "snowflake",
            "cache_strategy": "semantic_first",
            "account": "xy12345.us-east-1",
            "warehouse": "COMPUTE_WH",
            "database": "ANALYTICS",
            "schema": "PUBLIC",
            "role": "INTERLOCK_ANALYST",
            "user": "svc_interlock",
            "password": "",
            "password_ref": "env://SNOWFLAKE_PASSWORD",
            "private_key_ref": "env://SNOWFLAKE_PRIVATE_KEY",
            "authenticator": "snowflake",
            "application": "InterLock",
            "collect_query_history": "on",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200
    assert "data-sources-table" in resp.text

    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserted, "INSERT into data_sources was not executed"
    args = inserted[-1][1]
    assert args[1] == "sf_analytics"
    assert args[3] == "warehouse"
    assert args[5] == "semantic_first"

    cfg = args[4]
    assert cfg["account"] == "xy12345.us-east-1"
    assert cfg["warehouse"] == "COMPUTE_WH"
    assert cfg["database"] == "ANALYTICS"
    assert cfg["schema"] == "PUBLIC"
    assert cfg["role"] == "INTERLOCK_ANALYST"
    assert cfg["password_ref"] == "env://SNOWFLAKE_PASSWORD"
    assert cfg["private_key_ref"] == "env://SNOWFLAKE_PRIVATE_KEY"
    assert cfg["authenticator"] == "snowflake"
    assert cfg["application"] == "InterLock"
    assert cfg["collect_query_history"] == "on"
    assert "password" not in cfg

    metadata = args[6]
    assert metadata["connector_key"] == "snowflake"
    assert metadata["connector_family"] == "warehouse"
    assert metadata["connector_capabilities"]["supports_cost_metadata"] is True

    role_inserts = [c for c in pool._calls if "INSERT INTO source_roles" in c[1][0]]
    assert role_inserts, "default Snowflake source role templates were not created"


@pytest.mark.anyio
async def test_create_qdrant_source_persists_search_connector_config(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "qd_claims",
            "name": "Qdrant Claims",
            "source_type": "search",
            "connector_key": "qdrant",
            "cache_strategy": "semantic_first",
            "base_url": "http://qdrant:6333",
            "api_key_ref": "env://QDRANT_API_KEY",
            "collections": "claims,runbooks",
            "max_points_per_collection": "5",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200

    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    args = inserted[-1][1]
    assert args[1] == "qd_claims"
    assert args[3] == "search"
    cfg = args[4]
    assert cfg["base_url"] == "http://qdrant:6333"
    assert cfg["api_key_ref"] == "env://QDRANT_API_KEY"
    assert cfg["collections"] == "claims,runbooks"
    assert cfg["max_points_per_collection"] == "5"
    metadata = args[6]
    assert metadata["connector_key"] == "qdrant"
    assert metadata["connector_family"] == "search"


@pytest.mark.anyio
async def test_create_notion_source_persists_saas_connector_config(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "notion_knowledge",
            "name": "Notion Knowledge",
            "source_type": "saas",
            "connector_key": "notion",
            "cache_strategy": "semantic_first",
            "base_url": "https://api.notion.com",
            "workspace": "acme",
            "integration_token_ref": "env://NOTION_TOKEN",
            "notion_version": "2022-06-28",
            "max_results": "10",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200

    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    args = inserted[-1][1]
    assert args[1] == "notion_knowledge"
    assert args[3] == "saas"
    cfg = args[4]
    assert cfg["workspace"] == "acme"
    assert cfg["integration_token_ref"] == "env://NOTION_TOKEN"
    assert cfg["notion_version"] == "2022-06-28"
    assert cfg["max_results"] == "10"
    metadata = args[6]
    assert metadata["connector_key"] == "notion"
    assert metadata["connector_family"] == "saas"

    role_inserts = [c for c in pool._calls if "INSERT INTO source_roles" in c[1][0]]
    assert role_inserts, "default Notion source role templates were not created"


@pytest.mark.anyio
async def test_create_google_workspace_source_persists_hybrid_connector_config(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "workspace_knowledge",
            "name": "Google Workspace Knowledge",
            "source_type": "saas",
            "connector_key": "google_workspace",
            "cache_strategy": "semantic_first",
            "workspace_domain": "example.com",
            "enabled_services": "drive,gmail,calendar",
            "access_token_ref": "env://GOOGLE_WORKSPACE_TOKEN",
            "credentials_file_ref": "file:///run/secrets/gws-credentials.json",
            "drive_query": "trashed = false",
            "max_drive_files": "15",
            "gmail_users": "me,user@example.com",
            "gmail_labels": "INBOX,IMPORTANT",
            "gmail_query": "newer_than:30d",
            "max_gmail_messages": "10",
            "calendar_ids": "primary,team@example.com",
            "calendar_time_min": "2026-01-01T00:00:00Z",
            "calendar_time_max": "2026-12-31T23:59:59Z",
            "max_calendar_events": "20",
            "admin_report_applications": "drive,login",
            "admin_report_user_key": "all",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200

    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    args = inserted[-1][1]
    assert args[1] == "workspace_knowledge"
    assert args[3] == "saas"
    cfg = args[4]
    assert cfg["workspace_domain"] == "example.com"
    assert cfg["enabled_services"] == "drive,gmail,calendar"
    assert cfg["access_token_ref"] == "env://GOOGLE_WORKSPACE_TOKEN"
    assert cfg["credentials_file_ref"] == "file:///run/secrets/gws-credentials.json"
    assert cfg["drive_query"] == "trashed = false"
    assert cfg["gmail_users"] == "me,user@example.com"
    assert cfg["calendar_ids"] == "primary,team@example.com"
    assert "access_token" not in cfg

    metadata = args[6]
    assert metadata["connector_key"] == "google_workspace"
    assert metadata["connector_family"] == "workspace"

    role_inserts = [c for c in pool._calls if "INSERT INTO source_roles" in c[1][0]]
    assert role_inserts, "default Google Workspace source role templates were not created"


@pytest.mark.anyio
async def test_create_zendesk_source_persists_connector_config(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "zd_support",
            "name": "Zendesk Support",
            "source_type": "saas",
            "connector_key": "zendesk",
            "cache_strategy": "semantic_first",
            "subdomain": "acme",
            "email": "interlock@example.com",
            "api_token_ref": "env://ZENDESK_API_TOKEN",
            "brand_id": "10",
            "group_id": "20",
            "organization_id": "30",
            "status": "open",
            "include_users": "on",
            "include_organizations": "on",
            "include_ticket_comments": "on",
            "max_tickets": "50",
            "max_articles": "25",
            "max_users": "10",
            "max_organizations": "10",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200
    assert "data-sources-table" in resp.text

    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserted, "INSERT into data_sources was not executed"
    args = inserted[-1][1]
    assert args[1] == "zd_support"
    assert args[3] == "saas"

    cfg = args[4]
    assert cfg["subdomain"] == "acme"
    assert cfg["api_token_ref"] == "env://ZENDESK_API_TOKEN"
    assert cfg["brand_id"] == "10"
    assert cfg["group_id"] == "20"
    assert cfg["include_ticket_comments"] == "on"
    assert cfg["max_tickets"] == "50"
    assert "api_token" not in cfg

    metadata = args[6]
    assert metadata["connector_key"] == "zendesk"
    assert metadata["connector_family"] == "saas"
    assert metadata["connector_capabilities"]["supports_incremental_sync"] is True


@pytest.mark.anyio
async def test_create_github_source_persists_repo_config(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "gh_code",
            "name": "GitHub Code",
            "source_type": "saas",
            "connector_key": "github",
            "cache_strategy": "semantic_first",
            "base_url": "https://github.example.com/api/v3",
            "org": "acme",
            "repo_names": "app,api",
            "token_ref": "env://GITHUB_TOKEN",
            "ref": "main",
            "include_code_paths": "README.md\ndocs/runbook.md",
            "max_repos": "20",
            "max_issues_per_repo": "5",
            "max_prs_per_repo": "5",
            "max_releases_per_repo": "2",
            "max_workflows_per_repo": "4",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200
    assert "data-sources-table" in resp.text

    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserted, "INSERT into data_sources was not executed"
    args = inserted[-1][1]
    assert args[1] == "gh_code"
    assert args[3] == "saas"

    cfg = args[4]
    assert cfg["org"] == "acme"
    assert cfg["repo_names"] == "app,api"
    assert cfg["token_ref"] == "env://GITHUB_TOKEN"
    assert cfg["include_code_paths"] == "README.md\ndocs/runbook.md"
    assert cfg["max_prs_per_repo"] == "5"
    assert "token" not in cfg

    metadata = args[6]
    assert metadata["connector_key"] == "github"
    assert metadata["connector_family"] == "repo"


@pytest.mark.anyio
async def test_create_gitlab_source_persists_repo_config(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "gl_code",
            "name": "GitLab Code",
            "source_type": "saas",
            "connector_key": "gitlab",
            "cache_strategy": "semantic_first",
            "base_url": "https://gitlab.example.com",
            "group": "platform",
            "project": "platform/app",
            "token_ref": "env://GITLAB_TOKEN",
            "ref": "main",
            "include_code_paths": "README.md",
            "max_projects": "20",
            "max_issues_per_project": "5",
            "max_mrs_per_project": "5",
            "max_pipelines_per_project": "2",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200
    assert "data-sources-table" in resp.text

    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserted, "INSERT into data_sources was not executed"
    args = inserted[-1][1]
    cfg = args[4]
    assert cfg["group"] == "platform"
    assert cfg["project"] == "platform/app"
    assert cfg["token_ref"] == "env://GITLAB_TOKEN"
    assert cfg["max_mrs_per_project"] == "5"
    assert "token" not in cfg

    metadata = args[6]
    assert metadata["connector_key"] == "gitlab"
    assert metadata["connector_family"] == "repo"


@pytest.mark.anyio
async def test_create_slack_source_persists_collaboration_config(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "slack_knowledge",
            "name": "Slack Knowledge",
            "source_type": "collaboration",
            "connector_key": "slack",
            "cache_strategy": "semantic_first",
            "workspace": "acme",
            "bot_token_ref": "env://SLACK_BOT_TOKEN",
            "channel_types": "public_channel,private_channel",
            "include_messages": "on",
            "include_threads": "on",
            "include_files": "on",
            "max_channels": "20",
            "max_messages_per_channel": "15",
            "max_replies_per_thread": "10",
            "max_files": "5",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200
    assert "data-sources-table" in resp.text

    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert inserted, "INSERT into data_sources was not executed"
    args = inserted[-1][1]
    assert args[1] == "slack_knowledge"
    assert args[3] == "collaboration"

    cfg = args[4]
    assert cfg["workspace"] == "acme"
    assert cfg["bot_token_ref"] == "env://SLACK_BOT_TOKEN"
    assert cfg["include_threads"] == "on"
    assert cfg["max_messages_per_channel"] == "15"
    assert "bot_token" not in cfg

    metadata = args[6]
    assert metadata["connector_key"] == "slack"
    assert metadata["connector_family"] == "collaboration"
    assert metadata["connector_capabilities"]["supports_ingestion"] is True


@pytest.mark.anyio
async def test_create_endpoint_rejects_duplicate(client: AsyncClient, pool: FakePool) -> None:
    pool._existing_source = True
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "cache_strategy": "deterministic_first",
            "host": "postgres",
            "port": "5432",
            "database": "",
            "user": "",
            "password": "",
            "base_url": "",
        },
    )
    assert resp.status_code == 200
    # Returned the form again with an error banner.
    assert "already exists" in resp.text
    # The INSERT was NOT executed.
    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    assert not inserted


@pytest.mark.anyio
async def test_new_identity_form_renders(client: AsyncClient) -> None:
    resp = await client.get("/dashboard/access-control/identities/new")
    assert resp.status_code == 200
    assert "New Identity" in resp.text
    assert 'name="api_key"' in resp.text
    assert "Source access" in resp.text
    assert 'name="grant_source_id"' in resp.text
    assert 'name="grant_role_id"' in resp.text


@pytest.mark.anyio
async def test_create_identity_generates_and_reveals_key(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/access-control/identities/create",
        data={
            "name": "demo-agent",
            "agent_type": "custom",
            "team": "ops",
            "roles": "analyst,reader",
            "mapped_pg_role": "onyx",
            "generate_key": "on",
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert "Identity created" in body
    # The reveal panel contains a code block with a generated key.
    assert "<code>" in body
    assert "Copy" in body
    # INSERT executed.
    inserted = [c for c in pool._calls if "INSERT INTO identities" in c[1][0]]
    assert inserted


@pytest.mark.anyio
async def test_create_identity_uses_custom_api_key_when_generate_off(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/access-control/identities/create",
        data={
            "name": "custom-agent",
            "agent_type": "custom",
            "team": "",
            "roles": "analyst",
            "mapped_pg_role": "",
            "api_key": "my-chosen-key-1234567890abcdefABCDEF",
            # generate_key intentionally omitted (form unchecked).
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert "my-chosen-key-1234567890abcdefABCDEF" in body


@pytest.mark.anyio
async def test_rotate_identity_key_writes_safe_admin_audit(
    app,
    client: AsyncClient,
    pool: FakePool,
) -> None:
    old_hash = "old-api-key-hash"
    pool.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "name": "demo-agent",
            "roles": ["analyst"],
            "api_key_hash": old_hash,
        }
    )
    app.state.redis.delete = AsyncMock()

    resp = await client.post("/dashboard/access-control/identities/7/rotate-key")

    assert resp.status_code == 200
    assert "Identity created" in resp.text
    audit_calls = [c for c in pool._calls if "INSERT INTO admin_audit_log" in c[1][0]]
    assert audit_calls
    detail = audit_calls[-1][1][6]
    assert audit_calls[-1][1][3] == "identity.api_key.rotate"
    assert detail["before"]["name"] == "demo-agent"
    assert detail["after"]["rotated"] is True
    assert detail["after"]["cache_invalidated"] is True
    assert old_hash not in str(detail)
    assert "api_key" not in detail["after"]


@pytest.mark.anyio
async def test_create_identity_rejects_unsafe_custom_api_key(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/access-control/identities/create",
        data={
            "name": "unsafe-agent",
            "agent_type": "custom",
            "team": "",
            "roles": "analyst",
            "mapped_pg_role": "",
            "api_key": 'bad"<script>' + "x" * 32,
        },
    )

    assert resp.status_code == 200
    assert "Custom API keys must be" in resp.text
    assert not [c for c in pool._calls if "INSERT INTO identities" in c[1][0]]


@pytest.mark.anyio
async def test_create_identity_persists_source_scoped_roles(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/access-control/identities/create",
        content=urlencode(
            [
                ("name", "multi-source-agent"),
                ("agent_type", "custom"),
                ("team", "ops"),
                ("mapped_pg_role", ""),
                ("generate_key", "on"),
                ("grant_source_id", "pg_reporting"),
                ("grant_role", "read"),
                ("grant_source_id", "pg_billing"),
                ("grant_role", "analyst"),
                ("grant_source_id", "http_orders"),
                ("grant_role", "write"),
            ]
        ).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert resp.status_code == 200
    assert "Identity created" in resp.text
    assert "pg_reporting:read" in resp.text
    assert "pg_billing:analyst" in resp.text
    assert "http_orders:write" in resp.text

    inserted = [c for c in pool._calls if "INSERT INTO identities" in c[1][0]]
    assert inserted, "INSERT into identities was not executed"
    args = inserted[-1][1]
    assert args[4] == []
    metadata = args[10]
    assert metadata["source_roles_authority"] == "identity_source_role_grants"
    assert metadata["source_roles_derived"] is True
    assert metadata["source_roles"] == [
        {"source_id": "pg_reporting", "role": "read"},
        {"source_id": "pg_billing", "role": "analyst"},
        {"source_id": "http_orders", "role": "write"},
    ]


# ---------------------------------------------------------------------------
# Upstream TLS: the console can set it, and refuses what production refuses
# ---------------------------------------------------------------------------

_TLS_REQUIRED = "Verified upstream PostgreSQL TLS is required"


@pytest.fixture
async def production_client(pool: FakePool):
    app = _make_app(pool)
    app.state.config = app.state.config.model_copy(update={"environment": "production"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _pg_form(**tls: str) -> dict[str, str]:
    return {
        "source_id": "shop",
        "name": "Shop",
        "source_type": "postgresql",
        "connector_key": "postgresql",
        "cache_strategy": "deterministic_first",
        "host": "db.example.com",
        "port": "5432",
        "database": "shop",
        "user": "ro",
        "password_ref": "env://SHOP_PASSWORD",
        **tls,
    }


def _inserted_config(pool: FakePool) -> dict | None:
    inserted = [c for c in pool._calls if "INSERT INTO data_sources" in c[1][0]]
    return inserted[-1][1][4] if inserted else None


def _unhealthy_probe():
    from datetime import UTC, datetime

    from interlock.models import HealthStatus

    return AsyncMock(
        return_value=HealthStatus(
            source_id="<unsaved>",
            healthy=False,
            latency_ms=0.0,
            error="probe stubbed",
            checked_at=datetime.now(UTC),
        )
    )


@pytest.mark.anyio
async def test_new_source_form_offers_postgresql_tls_fields(client: AsyncClient) -> None:
    body = (await client.get("/dashboard/data-sources/new")).text
    assert 'name="sslmode"' in body
    assert 'name="ssl_ca"' in body
    assert "Production accepts only verify-full or verify-ca" in body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tls",
    [{}, {"sslmode": "require"}, {"sslmode": "disable"}],
    ids=["no-mode", "require", "disable"],
)
async def test_production_console_create_refuses_unverified_postgresql_tls(
    production_client: AsyncClient, pool: FakePool, tls: dict[str, str]
) -> None:
    resp = await production_client.post("/dashboard/data-sources/create", data=_pg_form(**tls))
    assert resp.status_code == 200
    assert 'class="form-error"' in resp.text
    assert _TLS_REQUIRED in resp.text
    assert _inserted_config(pool) is None


@pytest.mark.anyio
async def test_production_console_create_stores_verified_postgresql_tls(
    production_client: AsyncClient, pool: FakePool
) -> None:
    resp = await production_client.post(
        "/dashboard/data-sources/create",
        data=_pg_form(sslmode="verify-full", ssl_ca="/run/secrets/shop-ca/ca.crt"),
    )
    assert resp.status_code == 200
    assert "data-sources-table" in resp.text
    cfg = _inserted_config(pool)
    assert cfg is not None
    assert cfg["sslmode"] == "verify-full"
    assert cfg["ssl_ca"] == "/run/secrets/shop-ca/ca.crt"


@pytest.mark.anyio
async def test_development_console_create_still_accepts_a_local_postgresql(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post("/dashboard/data-sources/create", data=_pg_form(sslmode="disable"))
    assert resp.status_code == 200
    cfg = _inserted_config(pool)
    assert cfg is not None
    assert cfg["sslmode"] == "disable"


@pytest.mark.anyio
async def test_console_create_adds_tls_fields_to_postgresql_only(
    client: AsyncClient, pool: FakePool
) -> None:
    form = {
        **_pg_form(sslmode="verify-full", ssl_ca="/ca.crt"),
        "source_type": "mysql",
        "connector_key": "mysql",
        "port": "3306",
    }
    await client.post("/dashboard/data-sources/create", data=form)
    cfg = _inserted_config(pool)
    assert cfg is not None
    assert "sslmode" not in cfg
    assert "ssl_ca" not in cfg


@pytest.mark.anyio
async def test_production_console_test_connection_probes_with_the_tls_fields(
    production_client: AsyncClient,
) -> None:
    probe = _unhealthy_probe()
    with patch("interlock.admin.routes.dashboard.ConnectionManager.probe_unsaved", probe):
        await production_client.post(
            "/dashboard/data-sources/test",
            data=_pg_form(sslmode="verify-ca", ssl_ca="/ca.crt"),
        )
    cfg = probe.call_args.args[1]
    assert cfg["sslmode"] == "verify-ca"
    assert cfg["ssl_ca"] == "/ca.crt"
    assert probe.call_args.kwargs["allow_insecure_tls"] is False


@pytest.mark.anyio
async def test_console_create_generates_the_source_id_from_the_name(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "name": "Sales PostgreSQL",
            "source_type": "postgresql",
            "connector_key": "postgresql",
            "host": "db.example.com",
        },
    )
    assert resp.status_code == 200
    inserts = [
        c for c in pool._calls if c[0] == "execute" and "INSERT INTO data_sources" in c[1][0]
    ]
    assert inserts and inserts[-1][1][1] == "sales_postgresql"


@pytest.mark.anyio
async def test_console_create_refuses_a_malformed_supplied_source_id(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "Bad Id",
            "name": "Bad",
            "source_type": "postgresql",
            "connector_key": "postgresql",
            "host": "db.example.com",
        },
    )
    assert "source_id must be" in resp.text
    assert not [c for c in pool._calls if "INSERT INTO data_sources" in str(c[1][0])]


@pytest.mark.anyio
async def test_create_source_role_refuses_a_condition_the_evaluator_ignores(
    client: AsyncClient,
    pool: FakePool,
) -> None:
    """`columns` is not a constraint key: saved, it would have granted every column."""
    resp = await client.post(
        "/dashboard/data-sources/shop/roles",
        data={
            "role_key": "table_reader",
            "name": "Table Reader",
            "permission_effect": "allow",
            "permission_action": "db.table.select",
            "permission_resource_type": "db.table",
            "permission_resource_pattern": "public.customers.*",
            "permission_constraints": '{"columns":["id","email"]}',
        },
    )
    assert resp.status_code == 200
    assert "&#39;columns&#39; is not a condition InterLock checks" in resp.text
    assert not [c for c in pool._calls if "INSERT INTO source_role" in str(c[1][0])]


@pytest.mark.anyio
async def test_create_source_role_refuses_an_action_from_another_connector(
    client: AsyncClient,
    pool: FakePool,
) -> None:
    resp = await client.post(
        "/dashboard/data-sources/shop/roles",
        data={
            "role_key": "odd",
            "name": "Odd",
            "permission_effect": "allow",
            "permission_action": "storage.object.read",
            "permission_resource_type": "storage.object",
            "permission_resource_pattern": "*",
            "permission_constraints": "{}",
        },
    )
    assert "&#39;storage.object.read&#39; is not an action on this connector" in resp.text
    assert not [c for c in pool._calls if "INSERT INTO source_role" in str(c[1][0])]
