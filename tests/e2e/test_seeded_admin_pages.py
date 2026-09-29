"""Seeded Admin page certification.

These tests are intentionally HTTP-level rather than visual-browser tests. They
catch route drift, auth/session regressions, raw secret exposure, and shell
rendering issues in the same seeded Compose stack used by browser walkthroughs.
"""

from __future__ import annotations

import re

import pytest

ADMIN_PAGE_ROUTES = (
    ("/dashboard/overview", "Overview"),
    ("/dashboard/data-sources", "Data Sources"),
    ("/dashboard/connectors", "Connectors"),
    ("/dashboard/source-wizard", "Source Wizard"),
    ("/dashboard/access-control", "Identities"),
    ("/dashboard/access-control/identities", "Identities"),
    ("/dashboard/policies", "Policies"),
    ("/dashboard/ingestion", "Ingestion Jobs"),
    ("/dashboard/workers", "Worker Status"),
    ("/dashboard/discovery", "Discovery"),
    ("/dashboard/categories", "Category Browser"),
    ("/dashboard/entities", "Entity Explorer"),
    ("/dashboard/audit-costs", "Audit"),
    ("/dashboard/write-safety", "Write Safety"),
    ("/dashboard/proxy", "Proxy Monitor"),
    ("/dashboard/proxy-monitor", "Proxy Monitor"),
    ("/dashboard/policy-analytics", "Policy Analytics"),
    ("/dashboard/alerts", "Alerts"),
)

SEEDED_DETAIL_ROUTES = (
    ("/dashboard/data-sources/e2e_pg", "Configuration"),
    ("/dashboard/data-sources/e2e_pg/edit", "Edit Data Source"),
    ("/dashboard/data-sources/e2e_pg/roles", "Source Roles"),
    ("/dashboard/data-sources/e2e_pg/roles/new", "New Source Role"),
    ("/dashboard/data-sources/e2e_pg/roles/1/edit", "Edit Source Role"),
    ("/dashboard/access-control/identities/1", "Source roles"),
    ("/dashboard/access-control/identities/new", "Dedicated PG username"),
    ("/dashboard/policies/new", "New Policy"),
    ("/dashboard/ingestion/jobs/1", "Ingestion Job #1"),
    ("/dashboard/discovery/assets/1", "InterLock E2E Governance Runbook"),
    ("/dashboard/discovery/categories/engineering.runbooks", "engineering.runbooks"),
    ("/dashboard/discovery/entities/PRODUCT/InterLock", "InterLock"),
    ("/dashboard/audit-costs/events/9", "Audit Event"),
    ("/dashboard/alerts/new", "New Alert"),
)

SEEDED_SECRET_PATTERNS = (
    "e2e-admin-password",
    "ag-e2e-api-key",
    "ag-e2e-denied-key",
    "e2e-s3-secret-key",
    "e2e-pg-password",
    "source_pass",
)


@pytest.mark.e2e
def test_admin_primary_pages_render_shell_without_raw_secrets(admin_session) -> None:
    for route, expected_text in ADMIN_PAGE_ROUTES:
        response = admin_session.client.get(route, follow_redirects=True)
        body = response.text

        assert response.status_code == 200, route
        assert "text/html" in response.headers["content-type"], route
        assert "<!DOCTYPE html>" in body, route
        assert 'class="sidebar-logo' in body, route
        assert "context-data-lockup.svg" in body, route
        assert expected_text in body, route
        for pattern in SEEDED_SECRET_PATTERNS:
            assert pattern not in body, f"{pattern} leaked on {route}"

    for route, expected_text in SEEDED_DETAIL_ROUTES:
        response = admin_session.client.get(route, follow_redirects=True)
        body = response.text

        assert response.status_code == 200, route
        assert "text/html" in response.headers["content-type"], route
        assert "<!DOCTYPE html>" in body, route
        assert 'class="sidebar-logo' in body, route
        assert "context-data-lockup.svg" in body, route
        assert expected_text in body, route
        assert "Traceback" not in body, route
        assert '{"detail"' not in body, route
        for pattern in SEEDED_SECRET_PATTERNS:
            assert pattern not in body, f"{pattern} leaked on {route}"

    approvals = admin_session.client.get("/dashboard/write-safety", follow_redirects=True)
    approvals.raise_for_status()
    approval_links = re.findall(r'href="(/dashboard/write-safety/\d+)"', approvals.text)
    if approval_links:
        approval = admin_session.client.get(approval_links[0], follow_redirects=True)
        assert approval.status_code == 200
        assert "Approval" in approval.text
        assert "<!DOCTYPE html>" in approval.text
        for pattern in SEEDED_SECRET_PATTERNS:
            assert pattern not in approval.text

    detail = admin_session.client.get("/dashboard/data-sources/e2e_pg", follow_redirects=True)
    detail.raise_for_status()
    body = detail.text

    assert 'href="/dashboard/data-sources/e2e_pg/edit"' in body
    assert 'hx-post="/dashboard/data-sources/e2e_pg/test"' in body
    assert 'hx-post="/dashboard/data-sources/e2e_pg/toggle-enabled"' in body
    assert 'hx-post="/dashboard/data-sources/e2e_pg/invalidate-cache"' in body
    assert 'hx-post="/dashboard/data-sources/e2e_pg/delete"' in body

    edit = admin_session.client.get("/dashboard/data-sources/e2e_pg/edit", follow_redirects=True)
    edit.raise_for_status()
    edit_body = edit.text

    assert "Edit Data Source" in edit_body
    assert "Literal secrets are never rendered back into the page" in edit_body
    assert 'name="config_key" value="password"' not in edit_body
    assert "source_pass" not in edit_body

    overview = admin_session.client.get("/dashboard/overview", follow_redirects=True)
    overview.raise_for_status()
    assert "Healthy Workers" in overview.text
    assert "Queue Depth" in overview.text
    assert "Active Jobs" in overview.text
    assert "Stale Workers" in overview.text

    workers = admin_session.client.get("/dashboard/workers", follow_redirects=True)
    workers.raise_for_status()
    assert "Healthy Workers" in workers.text
    assert "Active Jobs" in workers.text
    assert "Queue Depth" in workers.text
    assert "Last Heartbeat" in workers.text

    alerts = admin_session.client.get("/dashboard/alerts", follow_redirects=True)
    alerts.raise_for_status()
    assert "Delivery is log-only unless a channel below is explicitly configured." in alerts.text
    assert "log-only until configured" in alerts.text
