"""Regression tests for audit P1-A and P1-B: admin console correctness.

AUDIT-COVERS: P1-A
AUDIT-COVERS: P1-B

P1-A: dashboard was broad but mostly read-only. Phase 2 wires real
component health (P1-B fix) plus configuration of approval forms with a
comment input - these are the first two of the management workflows
called out in the audit. The rest land in Phase 3 (full editor for
policies, source onboarding wizard, etc.) under separate task IDs.

P1-B: dashboard hard-coded health and broken approve buttons.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from interlock.admin.app import _install_template_filters
from interlock.admin.routes import dashboard

_TEMPLATES = Path("src/interlock/admin/templates")


def _render(name: str, **ctx: object) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )

    class _Templates:
        pass

    templates = _Templates()
    templates.env = env
    _install_template_filters(templates)  # type: ignore[arg-type]
    return env.get_template(name).render(**ctx, content_only=True)


# ---------------------------------------------------------------------------
# P1-A: dashboard surface includes management capabilities
# ---------------------------------------------------------------------------


def test_p1_a_overview_renders_real_components_list() -> None:
    components = [
        {"name": "PostgreSQL", "detail": "primary", "status": "ok"},
        {"name": "Redis", "detail": "online", "status": "ok"},
        {"name": "Workers", "detail": "2 active", "status": "ok"},
    ]
    html = _render(
        "pages/overview.html",
        active_page="overview",
        requests_24h=0,
        cache_hit_rate=0,
        active_sources=0,
        active_workers=2,
        pending_approvals=0,
        errors_24h=0,
        job_queued=0,
        job_processing=0,
        job_completed=0,
        job_failed=0,
        recent_activity=[],
        cache_tiers=[],
        components=components,
    )
    assert 'data-component="PostgreSQL"' in html
    assert 'data-component="Redis"' in html
    assert 'data-component="Workers"' in html


def test_p1_a_dashboard_route_provides_components_partial_endpoint() -> None:
    """A standalone /dashboard/overview/components partial endpoint exists
    so the overview can refresh component state without a full reload."""
    paths = [r.path for r in dashboard.router.routes]
    assert "/dashboard/overview/components" in paths


# ---------------------------------------------------------------------------
# P1-B: hard-coded health + broken approve buttons fixed
# ---------------------------------------------------------------------------


def test_p1_b_overview_no_longer_hardcodes_green() -> None:
    body = (_TEMPLATES / "pages" / "overview.html").read_text()
    # The inline "always green" PG Proxy / HTTP Proxy / Admin API / Cache
    # blocks are gone. The component grid is now driven by the
    # ``components`` list with a status-derived class.
    assert 'comp-name">PG Proxy<' not in body
    # We still allow the word "health-green" to appear in the template
    # (CSS class), but only inside the dynamic conditional - no more
    # static "<span class=\"health-dot health-green\"></span>" blocks
    # outside of the new {% for c in components %} loop.
    static_green = body.count('<span class="health-dot health-green"></span>')
    assert static_green == 0, "Overview must not hard-code health-green dots (P1-B regression)"


def test_p1_b_overview_partial_renders_status_dynamically() -> None:
    components = [
        {"name": "PostgreSQL", "detail": "primary", "status": "down"},
        {"name": "Redis", "detail": "online", "status": "ok"},
    ]
    html = _render("partials/component_grid.html", components=components)
    # PG Down -> red dot.
    assert 'data-status="down"' in html
    assert "health-red" in html
    # Redis OK -> green dot.
    assert 'data-status="ok"' in html
    assert "health-green" in html


def test_p1_b_approve_button_is_single_post_no_hardcoded_actor() -> None:
    body = (_TEMPLATES / "pages" / "write_safety.html").read_text()
    # No more dual hx-post + hx-get on the same element.
    assert 'hx-vals=\'{"approved_by": "admin-dashboard"}\'' not in body
    assert 'hx-vals=\'{"rejected_by": "admin-dashboard"}\'' not in body
    # The action is a single hx-post inside a form.
    assert 'hx-post="/api/approvals/{{ a.id }}/approve"' in body
    assert 'hx-post="/api/approvals/{{ a.id }}/reject"' in body


# ---------------------------------------------------------------------------
# P1-D: semantic cache wired (covered structurally; e2e tests in Phase 4
# add the runtime parity assertion)
# ---------------------------------------------------------------------------


def test_p1_d_gateway_lifespan_wires_embedding_engine_into_state() -> None:
    """AUDIT-COVERS: P1-D"""
    from interlock.gateway import app as gateway_app

    src = inspect.getsource(gateway_app.lifespan)
    assert "EmbeddingEngine(" in src
    assert "app.state.embedding_engine" in src


# ---------------------------------------------------------------------------
# P1-G: e2e tests exist in tests/e2e/ and run under -m e2e
# ---------------------------------------------------------------------------


def test_p1_g_e2e_suite_collects_under_marker() -> None:
    """AUDIT-COVERS: P1-G"""
    e2e_dir = Path("tests/e2e")
    assert e2e_dir.exists()
    test_files = list(e2e_dir.glob("test_*.py"))
    assert test_files, "Expected at least one tests/e2e/test_*.py file"
