"""Browser-level RBAC, HTMX, onboarding, approval, and audit certifications."""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.browser.support import (
    BROWSER_ADMIN_PASSWORD,
    assert_full_admin_shell,
    assert_no_secret_values,
    capture_evidence,
    wait_for_htmx,
)

pytestmark = [pytest.mark.browser, pytest.mark.e2e]


@pytest.mark.parametrize(
    ("username", "allowed_path", "denied_path"),
    (
        ("browser-auditor", "/dashboard/audit-costs", "/dashboard/data-sources"),
        ("browser-source-admin", "/dashboard/data-sources", "/dashboard/audit-costs"),
        ("browser-policy-admin", "/dashboard/policies", "/dashboard/write-safety"),
        (
            "browser-approval-reviewer",
            "/dashboard/write-safety",
            "/dashboard/access-control/identities",
        ),
    ),
)
def test_admin_read_rbac_is_enforced_in_real_browser_sessions(
    admin_page_factory: Any,
    browser_e2e_config: Any,
    username: str,
    allowed_path: str,
    denied_path: str,
) -> None:
    _, page, _ = admin_page_factory(username=username, password=BROWSER_ADMIN_PASSWORD)

    allowed = page.goto(f"{browser_e2e_config.admin_url}{allowed_path}")
    assert allowed is not None and allowed.status == 200
    denied = page.goto(f"{browser_e2e_config.admin_url}{denied_path}")
    assert denied is not None and denied.status == 403
    assert "does not allow" in page.locator("body").inner_text()


def test_source_onboarding_modal_uses_htmx_and_never_carries_inline_secret_in_dom(
    admin_page_factory: Any,
    browser_e2e_config: Any,
    browser_artifact_dir: Any,
) -> None:
    secret = "browser-inline-source-secret"
    _, page, diagnostics = admin_page_factory()
    page.goto(f"{browser_e2e_config.admin_url}/dashboard/data-sources")
    original_url = page.url

    page.get_by_role("button", name=re.compile("New Source")).click()
    wait_for_htmx(page)
    assert page.url == original_url
    assert page.locator('[role="dialog"][aria-modal="true"]').count() == 1

    # No source id is asked for: it is generated from the display name.
    assert page.locator('input[name="source_id"]').count() == 0
    page.locator('input[name="name"]').fill("Browser Certification Source")
    page.locator('select[name="connector_key"]').select_option("postgresql")
    page.get_by_role("button", name="Next").click()
    wait_for_htmx(page)

    page.locator('input[name="host"]').fill("source-postgres")
    page.locator('input[name="database"]').fill("source_db")
    page.locator('input[name="user"]').fill("source_user")
    page.locator('input[name="password"]').fill(secret)
    page.get_by_role("button", name="Next").click()
    wait_for_htmx(page)
    assert_no_secret_values(page, (secret,))

    page.get_by_role("button", name="Next").click()
    wait_for_htmx(page)
    page.get_by_role("button", name="Next").click()
    wait_for_htmx(page)
    assert "inline password supplied" in page.locator("body").inner_text().casefold()
    assert "browser_certification_source" in page.locator("body").inner_text()
    assert_no_secret_values(page, (secret,))
    capture_evidence(page, browser_artifact_dir, "source-onboarding-review-masked")

    page.get_by_role("button", name="Cancel").click()
    page.wait_for_url(re.compile(r".*/dashboard/data-sources$"))
    wait_for_htmx(page)
    assert page.locator('[role="dialog"]').count() == 0
    diagnostics.assert_clean("source onboarding modal")


def test_htmx_navigation_replaces_main_content_without_duplicating_shell(
    admin_page_factory: Any,
    browser_e2e_config: Any,
) -> None:
    _, page, diagnostics = admin_page_factory()
    page.goto(f"{browser_e2e_config.admin_url}/dashboard/data-sources")
    page.locator('a[href="/dashboard/data-sources/e2e_pg"]').first.click()
    page.wait_for_url(re.compile(r".*/dashboard/data-sources/e2e_pg$"))
    wait_for_htmx(page)

    # The point is that HTMX swapped the content without cloning the shell.
    # Assert on the shell itself: the logo count is 2 by design, one per theme.
    assert page.locator("nav.sidebar").count() == 1
    assert page.locator("#main-content").count() == 1
    assert "Configuration" in page.locator("#main-content").inner_text()
    diagnostics.assert_clean("HTMX source detail navigation")


def test_approval_detail_redacts_payload_and_rejects_through_htmx(
    admin_page_factory: Any,
    browser_e2e_config: Any,
    browser_artifact_dir: Any,
    seeded_browser_approval: tuple[int, str],
) -> None:
    approval_id, marker = seeded_browser_approval
    _, page, diagnostics = admin_page_factory(
        username="browser-approval-reviewer",
        password=BROWSER_ADMIN_PASSWORD,
    )
    page.goto(f"{browser_e2e_config.admin_url}/dashboard/write-safety/{approval_id}")

    assert_full_admin_shell(page, f"Approval #{approval_id}")
    assert marker not in page.locator("body").inner_text()
    assert "REDACTED" in page.locator("body").inner_text()
    assert_no_secret_values(page, (marker,))
    capture_evidence(page, browser_artifact_dir, "approval-detail-redacted")

    reject_form = page.locator('form[hx-post$="/reject"]')
    reject_form.locator('select[name="reason"]').select_option("unsafe")
    reject_form.locator('textarea[name="comment"]').fill("Rejected by browser certification")
    reject_form.get_by_role("button", name="Reject").click()
    page.wait_for_url(re.compile(r".*/dashboard/write-safety$"))
    wait_for_htmx(page)
    assert f"#{approval_id}" in page.locator("body").inner_text()
    assert "REJECTED" in page.locator("body").inner_text()
    diagnostics.assert_clean("approval rejection")


def test_audit_detail_is_shell_rendered_and_redacts_sensitive_metadata(
    admin_page_factory: Any,
    browser_e2e_config: Any,
    browser_artifact_dir: Any,
    seeded_browser_audit: tuple[int, str],
) -> None:
    audit_id, marker = seeded_browser_audit
    _, page, diagnostics = admin_page_factory(
        username="browser-auditor",
        password=BROWSER_ADMIN_PASSWORD,
    )
    response = page.goto(f"{browser_e2e_config.admin_url}/dashboard/audit-costs/events/{audit_id}")

    assert response is not None and response.status == 200
    assert_full_admin_shell(page, "Audit Event")
    assert marker not in page.locator("body").inner_text()
    assert "REDACTED" in page.locator("body").inner_text()
    assert_no_secret_values(page, (marker,))
    capture_evidence(page, browser_artifact_dir, "audit-detail-redacted")
    diagnostics.assert_clean("audit event detail")
