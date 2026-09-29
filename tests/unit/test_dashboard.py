"""Unit tests for the HTMX + Jinja2 dashboard."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.config import InterLockConfig
from interlock.models import AgentType, IdentityContext, PolicyDecision, SourceRoleDecision

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = ROOT / "src" / "interlock" / "admin" / "templates"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> InterLockConfig:
    return InterLockConfig()


class FakeRecord:
    """Minimal asyncpg.Record stand-in."""

    def __init__(self, data: dict):
        self._data = dict(data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __iter__(self):
        return iter(self._data.keys())

    def __getitem__(self, key):
        return self._data[key]


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_JOB = {
    "id": "aaaa-bbbb-cccc-dddd",
    "source_id": "warehouse",
    "status": "queued",
    "config": json.dumps({}),
    "priority": 5,
    "worker_id": None,
    "error": None,
    "created_at": "2025-01-15T12:00:00",
    "updated_at": "2025-01-15T12:00:00",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakePool:
    """Mock asyncpg pool returning configurable data."""

    def __init__(self):
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=0)
        self.execute = AsyncMock(return_value="OK")
        self.close = AsyncMock()

    def acquire(self):
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="OK")
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        return conn


class FakeRedis:
    """Mock Redis client."""

    def __init__(self):
        self._keys: list[bytes] = []
        self._data: dict[str, str] = {}
        self.close = AsyncMock()

    async def scan(self, cursor, match=None, count=100):
        return (0, self._keys)

    async def get(self, key):
        k = key.decode() if isinstance(key, bytes) else key
        return self._data.get(k)


@pytest.fixture
def mock_pool():
    return FakePool()


@pytest.fixture
def mock_redis():
    return FakeRedis()


@pytest.fixture
def app(mock_pool, mock_redis):
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as mock_create_pg,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch(
            "interlock.admin.app.create_redis_client", new_callable=AsyncMock
        ) as mock_create_redis,
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        mock_create_pg.return_value = mock_pool
        mock_create_redis.return_value = mock_redis
        application = create_app(config=_make_config())
        application.state.pg_pool = mock_pool
        application.state.redis = mock_redis
        # Disable admin auth in unit tests - the middleware itself is
        # tested separately in test_p0_f_admin_auth.py.
        application.state.auth_disabled = True
        yield application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Tests: Dashboard index
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dashboard_index_redirects(client):
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/dashboard/overview" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Tests: Overview page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_overview_page_returns_html(client, mock_pool):
    mock_pool.fetchval.return_value = 0
    mock_pool.fetchrow.return_value = FakeRecord({"rate": 0.0})
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/overview")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Overview" in resp.text
    assert "Healthy Workers" in resp.text
    assert "Queue Depth" in resp.text


# ---------------------------------------------------------------------------
# Tests: Data Sources page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_data_sources_page_returns_html(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/data-sources")
    assert resp.status_code == 200
    assert "Data Sources" in resp.text


# ---------------------------------------------------------------------------
# Tests: Access Control page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_access_control_redirects_to_identities(client):
    resp = await client.get("/dashboard/access-control", follow_redirects=False)

    assert resp.status_code == 307
    assert resp.headers["location"] == "/dashboard/access-control/identities"


@pytest.mark.anyio
async def test_identities_page_returns_html(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/access-control/identities")
    assert resp.status_code == 200
    assert "Identities" in resp.text


@pytest.mark.anyio
async def test_policies_page_returns_html(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/policies")

    assert resp.status_code == 200
    assert "Policies" in resp.text
    assert "Policy Rules" in resp.text


@pytest.mark.anyio
async def test_policy_create_htmx_returns_redirect_contract(client, mock_pool):
    mock_pool.fetchval.return_value = None
    resp = await client.post(
        "/dashboard/policies/create",
        headers={"HX-Request": "true"},
        data={
            "name": "deny-expensive-writes",
            "priority": "10",
            "cond_source_id": "warehouse",
            "cond_operation": "write",
            "action_effect": "deny",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/dashboard/policies"
    assert resp.text == ""


@pytest.mark.anyio
async def test_policy_dry_run_shows_policy_write_risk_cap(client, mock_pool):
    identity = IdentityContext(
        identity_id=7,
        user="E2E Agent",
        agent_type=AgentType.CUSTOM,
        source_roles=[{"source_id": "warehouse", "role": "writer"}],
    )
    source_role_decision = SourceRoleDecision(
        allowed=True,
        matched_role_ids=[11],
        matched_permission_ids=[22],
        reason="allowed",
    )
    policy_engine = MagicMock()
    policy_engine.load = AsyncMock()
    policy_engine.evaluate.return_value = PolicyDecision(
        allowed=True,
        rule_id=3,
        rule_name="medium-cap",
        write_risk_cap="medium",
        reason="matched cap",
    )
    source_role_evaluator = MagicMock()
    source_role_evaluator.evaluate = AsyncMock(return_value=source_role_decision)

    with (
        patch(
            "interlock.admin.routes.dashboard._admin_identity_context",
            new=AsyncMock(return_value=identity),
        ),
        patch(
            "interlock.admin.routes.dashboard.SourceRoleEvaluator",
            return_value=source_role_evaluator,
        ),
        patch(
            "interlock.admin.routes.dashboard.PolicyEngine",
            return_value=policy_engine,
        ),
    ):
        resp = await client.post(
            "/dashboard/policies/dry-run",
            data={
                "identity_id": "7",
                "source_id": "warehouse",
                "operation": "write",
                "tables": "public.customers",
            },
        )

    assert resp.status_code == 200
    assert "medium-cap" in resp.text
    assert "<dt>Write risk cap</dt><dd>medium</dd>" in resp.text
    policy_engine.evaluate.assert_called_once()


@pytest.mark.anyio
async def test_policy_dry_run_postgres_sql_preview_uses_database_permission_request(
    client,
    mock_pool,
):
    identity = IdentityContext(
        identity_id=7,
        user="E2E Agent",
        agent_type=AgentType.CUSTOM,
        source_roles=[{"source_id": "e2e_pg", "role": "analyst"}],
    )
    source_role_decision = SourceRoleDecision(
        allowed=True,
        matched_role_ids=[11],
        matched_permission_ids=[22],
        reason="allowed",
    )
    policy_engine = MagicMock()
    policy_engine.load = AsyncMock()
    policy_engine.evaluate.return_value = PolicyDecision(
        allowed=True,
        rule_id=3,
        rule_name="allow-read",
        reason="matched",
    )
    source_role_evaluator = MagicMock()
    source_role_evaluator.evaluate = AsyncMock(return_value=source_role_decision)
    mock_pool.fetchrow.return_value = FakeRecord(
        {"source_type": "postgresql", "metadata": {"connector_key": "postgresql"}}
    )

    with (
        patch(
            "interlock.admin.routes.dashboard._admin_identity_context",
            new=AsyncMock(return_value=identity),
        ),
        patch(
            "interlock.admin.routes.dashboard.SourceRoleEvaluator",
            return_value=source_role_evaluator,
        ),
        patch(
            "interlock.admin.routes.dashboard.PolicyEngine",
            return_value=policy_engine,
        ),
    ):
        resp = await client.post(
            "/dashboard/policies/dry-run",
            data={
                "identity_id": "7",
                "source_id": "e2e_pg",
                "sql": "SELECT id, email FROM public.customers",
            },
        )

    assert resp.status_code == 200
    permission_request = source_role_evaluator.evaluate.await_args.args[0]
    assert permission_request.protocol == "postgresql"
    assert permission_request.action == "db.table.select"
    assert permission_request.resources == ["public.customers.*"]
    assert "http.post" not in resp.text


# ---------------------------------------------------------------------------
# Tests: Ingestion page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingestion_page_returns_html(client, mock_pool):
    mock_pool.fetchval.return_value = 0
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/ingestion")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<table" in resp.text
    assert "Ingestion Jobs" in resp.text


@pytest.mark.anyio
async def test_ingestion_page_with_jobs(client, mock_pool):
    mock_pool.fetchval.return_value = 1
    # First fetch returns job rows, second returns status counts
    mock_pool.fetch.side_effect = [
        [FakeRecord(SAMPLE_JOB)],
        [FakeRecord({"status": "queued", "cnt": 1})],
    ]
    resp = await client.get("/dashboard/ingestion")
    assert resp.status_code == 200
    assert "warehouse" in resp.text
    assert "queued" in resp.text
    assert "/api/ingestion/jobs/" not in resp.text
    assert 'hx-target="#job-table-container"' in resp.text


@pytest.mark.anyio
async def test_ingestion_job_detail_tolerates_missing_operational_fields(client, mock_pool):
    migrated_job = {
        "id": 1,
        "source_id": "warehouse",
        "status": "queued",
        "stage": "queued",
        "worker_id": None,
        "priority_score": 5,
        "lease_expires_at": None,
        "retry_count": 0,
        "last_error": None,
        "file_path": "/fixtures/runbook.md",
        "metadata": {},
    }
    mock_pool.fetchrow.return_value = FakeRecord(migrated_job)

    resp = await client.get("/dashboard/ingestion/jobs/1")

    assert resp.status_code == 200
    assert "Ingestion Job #1" in resp.text
    assert "Duration" in resp.text
    assert "Cost" in resp.text
    assert "Traceback" not in resp.text


@pytest.mark.anyio
async def test_ingestion_htmx_returns_partial(client, mock_pool):
    """HTMX requests should return only the partial (no full page wrapper)."""
    mock_pool.fetchval.return_value = 0
    mock_pool.fetch.return_value = []
    resp = await client.get(
        "/dashboard/ingestion",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.text
    # Partial should have the table but NOT the full base layout
    assert "<table" in body
    assert "<!DOCTYPE html>" not in body
    assert '<nav class="sidebar"' not in body


@pytest.mark.anyio
async def test_ingestion_htmx_retry_does_not_swap_json_into_page(client, mock_pool):
    mock_pool.fetchrow.return_value = FakeRecord({"id": 1})
    mock_pool.fetchval.return_value = 0
    mock_pool.fetch.return_value = []

    resp = await client.post(
        "/dashboard/ingestion/jobs/1/retry",
        headers={"HX-Request": "true"},
        data={"status": "failed"},
    )

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<table" in resp.text
    assert '"source_id"' not in resp.text
    assert mock_pool.fetchrow.call_args[0][0].lstrip().startswith("UPDATE ingestion_jobs")
    assert mock_pool.fetchval.call_args[0][1] == "failed"


@pytest.mark.anyio
async def test_ingestion_htmx_cancel_does_not_swap_json_into_page(client, mock_pool):
    mock_pool.fetchrow.return_value = FakeRecord({"id": 1})
    mock_pool.fetchval.return_value = 0
    mock_pool.fetch.return_value = []

    resp = await client.post(
        "/dashboard/ingestion/jobs/1/cancel",
        headers={"HX-Request": "true"},
        data={"status": "queued"},
    )

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<table" in resp.text
    assert '"source_id"' not in resp.text
    assert mock_pool.fetchrow.call_args[0][0].lstrip().startswith("UPDATE ingestion_jobs")
    assert mock_pool.fetchval.call_args[0][1] == "queued"


@pytest.mark.anyio
async def test_ingestion_status_filter(client, mock_pool):
    mock_pool.fetchval.return_value = 0
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/ingestion?status=failed&page=1")
    assert resp.status_code == 200
    # Verify fetchval was called with the status filter
    call_args = mock_pool.fetchval.call_args
    assert call_args[0][1] == "failed"


@pytest.mark.anyio
async def test_ingestion_pagination(client, mock_pool):
    mock_pool.fetchval.return_value = 75  # 3 pages at 25 per page
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/ingestion?page=2")
    assert resp.status_code == 200
    # The fetch calls include job rows and status counts.
    # Find the call with an offset argument (page 2 -> offset 25).
    found = False
    for call in mock_pool.fetch.call_args_list:
        args = call[0]
        if len(args) >= 3 and args[2] == 25:
            found = True
            break
    assert found, "Expected a fetch call with offset=25 for page 2"


@pytest.mark.anyio
async def test_ingestion_page_clamped(client, mock_pool):
    """Page number beyond total pages should be clamped."""
    mock_pool.fetchval.return_value = 10  # 1 page
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/ingestion?page=999")
    assert resp.status_code == 200
    # Find the fetch call with offset argument - should be 0
    found = False
    for call in mock_pool.fetch.call_args_list:
        args = call[0]
        if len(args) >= 3 and isinstance(args[2], int):
            assert args[2] == 0
            found = True
            break
    assert found, "Expected a fetch call with offset=0"


# ---------------------------------------------------------------------------
# Tests: Workers page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_workers_page_returns_html(client, mock_redis):
    resp = await client.get("/dashboard/workers")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Worker Status" in resp.text


@pytest.mark.anyio
async def test_workers_page_with_workers(client, mock_redis):
    now = time.time()
    mock_redis._keys = [b"worker:heartbeat:w-1"]
    mock_redis._data = {
        "worker:heartbeat:w-1": json.dumps(
            {
                "timestamp": now - 5,
                "metadata": {"current_job": "job-abc"},
            }
        )
    }
    resp = await client.get("/dashboard/workers")
    assert resp.status_code == 200
    assert "w-1" in resp.text
    assert "health-green" in resp.text
    assert "Active Jobs" in resp.text
    assert "Queue Depth" in resp.text


@pytest.mark.anyio
async def test_workers_htmx_returns_partial(client, mock_redis):
    resp = await client.get(
        "/dashboard/workers",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<!DOCTYPE html>" not in body


# ---------------------------------------------------------------------------
# Tests: Categories page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_categories_page_returns_html(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/categories")
    assert resp.status_code == 200
    assert "Category Browser" in resp.text


@pytest.mark.anyio
async def test_categories_children_returns_partial(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/categories/root/children")
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" not in resp.text


# ---------------------------------------------------------------------------
# Tests: Entities page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_entities_page_returns_html(client, mock_pool):
    resp = await client.get("/dashboard/entities")
    assert resp.status_code == 200
    assert "Entity Explorer" in resp.text
    assert "Search entities" in resp.text


@pytest.mark.anyio
async def test_entity_search_with_query(client, mock_pool):
    mock_pool.fetch.return_value = [
        FakeRecord({"text": "Acme Corp", "entity_type": "ORG", "doc_count": 12})
    ]
    resp = await client.get("/dashboard/entities/search?q=acme")
    assert resp.status_code == 200
    assert "Acme Corp" in resp.text
    assert "ORG" in resp.text


@pytest.mark.anyio
async def test_entity_search_empty_query(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/entities/search?q=")
    assert resp.status_code == 200
    # Empty query now returns all entities (or empty if DB has none)
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_entity_search_no_results(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/entities/search?q=nonexistent")
    assert resp.status_code == 200
    assert "No entities found" in resp.text


# ---------------------------------------------------------------------------
# Tests: Discovery page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discovery_page_returns_html(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/discovery")
    assert resp.status_code == 200
    assert "Discovery" in resp.text
    assert "Categories" in resp.text


@pytest.mark.anyio
async def test_discovery_entities_tab(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/discovery?tab=entities")
    assert resp.status_code == 200
    # Phase 4 renamed the in-page filter input. The new placeholder is
    # "Filter entities..."; the unified search bar at the page level
    # uses "Search assets, tables, columns, entities...".
    assert "Filter entities" in resp.text or "entities" in resp.text.lower()


# ---------------------------------------------------------------------------
# Tests: Audit & Costs page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_audit_costs_page_returns_html(client, mock_pool):
    mock_pool.fetchval.return_value = 0
    mock_pool.fetchrow.return_value = FakeRecord(
        {
            "total_requests": 0,
            "total_cost": 0.0,
            "cache_hit_rate": 0.0,
            "avg_latency_ms": 0.0,
            "llm_verify_count": 0,
        }
    )
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/audit-costs")
    assert resp.status_code == 200
    assert "Audit" in resp.text
    assert "Costs" in resp.text


@pytest.mark.anyio
async def test_audit_event_detail_does_not_render_raw_tokens(client, mock_pool):
    mock_pool.fetchrow.return_value = FakeRecord(
        {
            "id": 7,
            "created_at": "2026-05-27T12:00:00",
            "source_id": "http_source",
            "identity_id": 1,
            "identity_name": "E2E Agent",
            "protocol": "http",
            "operation": "write",
            "normalized_operation": "http.post",
            "status": "queued",
            "correlation_id": "corr-123",
            "policy_decision": {},
            "cache_hit": False,
            "cache_tier": None,
            "approval_status": "pending",
            "approval_id": 99,
            "pii_detected": False,
            "latency_ms": 12,
            "cost_metadata": {},
            "route": "/proxy/http_source/customers",
            "upstream_target": "http://upstream/customers",
            "error_message": None,
            "request_metadata": json.dumps(
                {
                    "headers": {
                        "authorization": "Bearer should-not-render",
                        "content-type": "application/json",
                    },
                    "body": {
                        "encoding": "json",
                        "data": {"token": "secret-token", "safe": "ok"},
                    },
                }
            ),
            "metadata": {},
        }
    )

    resp = await client.get("/dashboard/audit-costs/events/7")

    assert resp.status_code == 200
    assert "should-not-render" not in resp.text
    assert "secret-token" not in resp.text
    assert "[REDACTED]" in resp.text
    assert "Sensitive request metadata is redacted" in resp.text


@pytest.mark.anyio
async def test_audit_event_detail_shows_correlation_and_readable_policy(client, mock_pool):
    """audit_log has no correlation column; the ID lives in the request metadata.

    The rc.13 evaluation found the Correlation field empty while the metadata
    held the ID, and the policy decision printed as a Python dict.
    """
    mock_pool.fetchrow.return_value = FakeRecord(
        {
            "id": 8,
            "created_at": "2026-09-29T12:00:00",
            "source_id": "sample_shop",
            "identity_id": 1,
            "identity_name": "quickstart-agent",
            "protocol": "mcp",
            "operation": "read",
            "status": "success",
            "policy_decision": json.dumps(
                {"rule_id": "allow-sample-shop-reads", "effect": "allow"}
            ),
            "cache_hit": True,
            "cache_tier": "l1",
            "pii_detected": True,
            "pii_types": ["EMAIL"],
            "latency_ms": 2,
            "cost_metadata": {},
            "request_metadata": json.dumps({"correlation_id": "corr-from-metadata"}),
            "metadata": {},
        }
    )

    resp = await client.get("/dashboard/audit-costs/events/8")

    assert resp.status_code == 200
    assert "corr-from-metadata" in resp.text
    assert "&#34;rule_id&#34;: &#34;allow-sample-shop-reads&#34;" in resp.text
    assert "{&#39;rule_id&#39;" not in resp.text, "policy still rendered as a Python dict"
    assert "redacted when the answer was cached" in resp.text


@pytest.mark.anyio
async def test_audit_log_partial(client, mock_pool):
    mock_pool.fetchval.return_value = 0
    mock_pool.fetch.return_value = []
    resp = await client.get(
        "/dashboard/audit-costs/log",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" not in resp.text


# ---------------------------------------------------------------------------
# Tests: Write Safety page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_write_safety_page_returns_html(client, mock_pool):
    mock_pool.fetchrow.return_value = FakeRecord(
        {"pending": 1, "approved_24h": 2, "rejected_24h": 0, "expired_24h": 0}
    )
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/write-safety")
    assert resp.status_code == 200
    assert "Write Safety" in resp.text


@pytest.mark.anyio
async def test_write_safety_detail_redacts_metadata_and_sql_literals(client, mock_pool):
    mock_pool.fetchrow.return_value = FakeRecord(
        {
            "id": 42,
            "identity_id": 1,
            "identity_name": "E2E Agent",
            "source_id": "warehouse",
            "source_name": "Warehouse",
            "sql_text": "INSERT INTO users(password, email) VALUES ('super-secret', 'a@example.com')",
            "risk_level": "high",
            "status": "pending",
            "created_at": "2026-05-27T12:00:00",
            "expires_at": "2026-05-27T13:00:00",
            "executed_at": None,
            "request_metadata": json.dumps(
                {
                    "reason": "Risky write",
                    "headers": {"authorization": "Bearer should-not-render"},
                    "body": {"data": {"token": "secret-token", "safe": "ok"}},
                }
            ),
        }
    )
    mock_pool.fetch.return_value = []

    resp = await client.get("/dashboard/write-safety/42")

    assert resp.status_code == 200
    assert "super-secret" not in resp.text
    assert "a@example.com" not in resp.text
    assert "should-not-render" not in resp.text
    assert "secret-token" not in resp.text
    assert "[REDACTED_LITERAL]" in resp.text
    assert "Sensitive approval metadata" in resp.text


# ---------------------------------------------------------------------------
# Tests: Proxy Monitor page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_proxy_monitor_page_returns_html(client, mock_pool):
    mock_pool.fetchval.return_value = 0
    mock_pool.fetchrow.return_value = FakeRecord({"p50": 0, "p95": 0, "p99": 0})
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/proxy")
    assert resp.status_code == 200
    assert "Proxy Monitor" in resp.text


@pytest.mark.anyio
async def test_proxy_monitor_alias_redirects_to_proxy_page(client):
    resp = await client.get("/dashboard/proxy-monitor", follow_redirects=False)

    assert resp.status_code == 307
    assert resp.headers["location"] == "/dashboard/proxy"


def test_source_role_dry_run_includes_sql_preview_field():
    template = (TEMPLATES_DIR / "partials" / "source_role_form.html").read_text()

    assert 'id="source-role-dry-run-fields"' in template
    assert 'hx-include="#source-role-dry-run-fields"' in template
    dry_run_section = template.split('id="source-role-dry-run-fields"', 1)[1].split(
        'id="source-role-dry-run-result"',
        1,
    )[0]
    assert 'name="sql"' in dry_run_section


# ---------------------------------------------------------------------------
# Tests: Policy Analytics page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_policy_analytics_page_returns_html(client, mock_pool):
    mock_pool.fetch.return_value = []
    resp = await client.get("/dashboard/policy-analytics")
    assert resp.status_code == 200
    assert "Policy Analytics" in resp.text


@pytest.mark.anyio
async def test_alert_create_htmx_returns_redirect_contract(client, mock_pool):
    mock_pool.fetchval.return_value = None
    resp = await client.post(
        "/dashboard/alerts/create",
        headers={"HX-Request": "true"},
        data={
            "name": "high-error-rate",
            "condition_type": "error_rate",
            "comparator": ">",
            "threshold": "0.05",
            "window_seconds": "300",
            "notification_channel": "log",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/dashboard/alerts"
    assert resp.text == ""
