"""Desktop/mobile Admin shell, security-header, overflow, and keyboard checks."""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.browser.support import (
    FORM_ADMIN_ROUTES,
    PRIMARY_ADMIN_ROUTES,
    assert_form_controls_are_styled,
    assert_full_admin_shell,
    assert_no_clipped_content,
    assert_no_overflowing_children,
    assert_no_page_overflow,
    assert_no_secret_values,
    assert_security_headers,
    assert_text_contrast_aa,
    assert_webfonts_loaded,
    capture_evidence,
    set_theme,
    wait_for_htmx,
)

pytestmark = [pytest.mark.browser, pytest.mark.e2e]


@pytest.mark.parametrize(
    ("viewport_name", "viewport"),
    (
        ("desktop", {"width": 1440, "height": 1000}),
        ("mobile-390", {"width": 390, "height": 844}),
    ),
)
def test_admin_primary_pages_certify_shell_csp_overflow_and_secret_dom(
    admin_page_factory: Any,
    browser_e2e_config: Any,
    browser_artifact_dir: Any,
    viewport_name: str,
    viewport: dict[str, int],
) -> None:
    _, page, diagnostics = admin_page_factory(viewport=viewport)

    for name, route, expected_text in PRIMARY_ADMIN_ROUTES:
        diagnostics.reset()
        response = page.goto(
            f"{browser_e2e_config.admin_url}{route}", wait_until="domcontentloaded"
        )
        wait_for_htmx(page)

        assert response is not None and response.status == 200, route
        assert_security_headers(response, route)
        assert_full_admin_shell(page, expected_text)
        assert_no_page_overflow(page, route)
        assert_no_clipped_content(page, route)
        assert_no_secret_values(page)
        diagnostics.assert_clean(route)
        capture_evidence(page, browser_artifact_dir, f"{viewport_name}-{name}")


def test_admin_keyboard_navigation_reaches_primary_actions_and_traps_modal_focus(
    admin_page_factory: Any,
    browser_e2e_config: Any,
) -> None:
    _, page, diagnostics = admin_page_factory(viewport={"width": 390, "height": 844})
    page.goto(f"{browser_e2e_config.admin_url}/dashboard/data-sources")

    page.locator("body").press("Tab")
    active = page.evaluate("""() => ({tag: document.activeElement.tagName, visible:
        Boolean(document.activeElement.offsetWidth || document.activeElement.offsetHeight)})""")
    assert active["tag"] in {"A", "BUTTON", "INPUT", "SELECT"}
    assert active["visible"] is True

    trigger = page.get_by_role("button", name=re.compile("New Source"))
    trigger.click()
    wait_for_htmx(page)
    dialog = page.locator('[role="dialog"][aria-modal="true"]')
    assert dialog.count() == 1
    assert dialog.locator(":focus").count() == 1

    page.keyboard.press("Shift+Tab")
    focused_inside = page.evaluate("""() => Boolean(document.querySelector('[role="dialog"]')
          ?.contains(document.activeElement))""")
    assert focused_inside is True

    page.keyboard.press("Escape")
    assert dialog.count() == 0
    assert page.evaluate("() => document.activeElement?.textContent.includes('New Source')") is True
    diagnostics.assert_clean("source modal keyboard workflow")


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_admin_primary_pages_meet_contrast_aa_in_both_themes(
    admin_page_factory: Any,
    browser_e2e_config: Any,
    browser_artifact_dir: Any,
    theme: str,
) -> None:
    """Every page must be legible in both themes.

    Before the console had a token system this page set carried 45 elements
    below AA on the source-detail route alone, including the primary sidebar
    navigation at 4.21:1.
    """
    _, page, diagnostics = admin_page_factory(viewport={"width": 1440, "height": 1000})
    set_theme(page, browser_e2e_config.admin_url, theme)

    for name, route, _expected in PRIMARY_ADMIN_ROUTES:
        diagnostics.reset()
        response = page.goto(
            f"{browser_e2e_config.admin_url}{route}", wait_until="domcontentloaded"
        )
        wait_for_htmx(page)
        assert response is not None and response.status == 200, route

        rendered = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        assert rendered == theme, f"{route}: expected data-theme={theme}, got {rendered}"

        assert_text_contrast_aa(page, route, theme)
        capture_evidence(page, browser_artifact_dir, f"contrast-{theme}-{name}")


def test_admin_ships_the_typeface_it_declares(
    admin_page_factory: Any,
    browser_e2e_config: Any,
) -> None:
    """Guards the regression where a face was declared but never loaded."""
    _, page, _diagnostics = admin_page_factory(viewport={"width": 1440, "height": 1000})
    page.goto(f"{browser_e2e_config.admin_url}/dashboard/overview", wait_until="load")
    page.wait_for_function("() => document.fonts.status === 'loaded'")

    assert_webfonts_loaded(page, "/dashboard/overview")


def test_admin_mobile_navigation_reaches_every_destination(
    admin_page_factory: Any,
    browser_e2e_config: Any,
) -> None:
    """At mobile width the drawer must expose every nav item.

    The sidebar previously collapsed to a horizontal strip whose later entries
    scrolled off-screen with no way to reach them.
    """
    _, page, _diagnostics = admin_page_factory(viewport={"width": 390, "height": 844})
    page.goto(f"{browser_e2e_config.admin_url}/dashboard/overview", wait_until="domcontentloaded")
    wait_for_htmx(page)

    page.locator(".js-nav-toggle").click()
    page.wait_for_selector("#primary-nav.is-open")
    # The drawer slides in; measure only once the transform has settled.
    page.wait_for_function(
        "() => document.getElementById('primary-nav').getBoundingClientRect().left >= -1"
    )

    links = page.locator("#primary-nav .nav-links a")
    total = links.count()
    assert total >= 13, f"expected the full nav, found {total} links"

    viewport_width = page.viewport_size["width"]
    for index in range(total):
        box = links.nth(index).bounding_box()
        assert box is not None, f"nav link {index} is not rendered"
        assert box["x"] >= -1, f"nav link {index} sits off the left edge: {box}"
        assert (
            box["x"] + box["width"] <= viewport_width + 1
        ), f"nav link {index} is unreachable past the right edge: {box}"


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_admin_form_pages_are_styled_and_legible(
    admin_page_factory: Any,
    browser_e2e_config: Any,
    browser_artifact_dir: Any,
    theme: str,
) -> None:
    """Form pages get the same certification as destination pages.

    They were previously uncovered, which is how every untyped text input in
    the console kept the browser's default styling unnoticed.
    """
    _, page, diagnostics = admin_page_factory(viewport={"width": 1440, "height": 1000})
    set_theme(page, browser_e2e_config.admin_url, theme)

    for name, route, _expected in FORM_ADMIN_ROUTES:
        diagnostics.reset()
        response = page.goto(
            f"{browser_e2e_config.admin_url}{route}", wait_until="domcontentloaded"
        )
        wait_for_htmx(page)
        assert response is not None and response.status == 200, route

        assert_form_controls_are_styled(page, route)
        assert_text_contrast_aa(page, route, theme)
        assert_no_clipped_content(page, route)
        diagnostics.assert_clean(route)
        capture_evidence(page, browser_artifact_dir, f"form-{theme}-{name}")


@pytest.mark.parametrize(
    ("viewport_name", "viewport"),
    (
        ("desktop", {"width": 1440, "height": 1000}),
        ("mobile-390", {"width": 390, "height": 844}),
    ),
)
def test_admin_detail_pages_keep_their_content_inside_their_panels(
    admin_page_factory: Any,
    browser_e2e_config: Any,
    browser_artifact_dir: Any,
    seeded_browser_identity_id: int,
    seeded_browser_approval: tuple[int, str],
    seeded_browser_audit: tuple[int, str],
    seeded_browser_ingestion_job: int,
    viewport_name: str,
    viewport: dict[str, int],
) -> None:
    """Detail pages were never certified, which is how the bleeding shipped.

    The identity detail page laid five cards into 300px tracks, so its grants
    table and API Key copy painted over the neighbouring panels at 1440px. The
    primary-route walk never opened it, and the page-level overflow check
    cannot see a panel bleeding into the one beside it.
    """
    approval_id, _ = seeded_browser_approval
    audit_id, _ = seeded_browser_audit
    routes = (
        ("identity-detail", f"/dashboard/access-control/identities/{seeded_browser_identity_id}"),
        ("source-detail", f"/dashboard/data-sources/{browser_e2e_config.source_id_pg}"),
        ("audit-event-detail", f"/dashboard/audit-costs/events/{audit_id}"),
        ("write-safety-detail", f"/dashboard/write-safety/{approval_id}"),
        ("ingestion-job-detail", f"/dashboard/ingestion/jobs/{seeded_browser_ingestion_job}"),
    )

    _, page, diagnostics = admin_page_factory(viewport=viewport)
    for name, route in routes:
        diagnostics.reset()
        response = page.goto(
            f"{browser_e2e_config.admin_url}{route}", wait_until="domcontentloaded"
        )
        wait_for_htmx(page)

        assert response is not None and response.status == 200, route
        assert_no_page_overflow(page, route)
        assert_no_clipped_content(page, route)
        assert_no_overflowing_children(page, route)
        assert_no_secret_values(page)
        diagnostics.assert_clean(route)
        capture_evidence(page, browser_artifact_dir, f"{viewport_name}-{name}")


_BLEEDING_PANEL_FIXTURE = """
<!doctype html>
<html><head><style>
  body { margin: 0; width: 600px; }
  .card { width: 260px; border: 1px solid #888; }
  .table-wrapper { overflow-x: auto; }
  table { width: 520px; border-collapse: collapse; }
  td { white-space: nowrap; }
</style></head>
<body><main><div class="card">{{BODY}}</div></main></body></html>
"""


@pytest.mark.parametrize(
    ("case", "body", "expect_offender"),
    (
        ("unwrapped", "<table><tr><td>a wide cell that does not fit</td></tr></table>", True),
        (
            "wrapped",
            '<div class="table-wrapper"><table><tr>'
            "<td>a wide cell that does not fit</td></tr></table></div>",
            False,
        ),
    ),
)
def test_the_overflow_guard_detects_a_panel_painting_outside_itself(
    browser_engine: Any,
    case: str,
    body: str,
    expect_offender: bool,
) -> None:
    """Prove the guard itself, on markup built to bleed.

    `assert_no_overflowing_children` is the assertion that was missing when the
    detail pages shipped their bleeding, so it has to be shown to fail on a
    card whose table is wider than the card and passes it once the table sits
    in a scrolling `.table-wrapper`. This runs against synthetic markup rather
    than the console so it keeps proving the mechanism after the pages are
    fixed - a check against the live pages can only ever pass.
    """
    context = browser_engine.new_context(viewport={"width": 600, "height": 400})
    try:
        page = context.new_page()
        page.set_content(_BLEEDING_PANEL_FIXTURE.replace("{{BODY}}", body))
        if expect_offender:
            with pytest.raises(AssertionError, match="painted outside its panel"):
                assert_no_overflowing_children(page, f"synthetic:{case}")
        else:
            assert_no_overflowing_children(page, f"synthetic:{case}")
    finally:
        context.close()
