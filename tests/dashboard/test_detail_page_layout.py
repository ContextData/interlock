"""Detail pages are a single stack of full-width, table-aligned sections.

The identity detail page used to render five cards into
`repeat(auto-fit, minmax(300px, 1fr))`. A card is not sized by its widest
child - nothing reset its automatic minimum width - so the grants table and the
API Key copy painted across the neighbouring panels on a wide screen. These
tests pin the structure that replaced it; `tests/browser` proves the pixels.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.templating import Jinja2Templates

from interlock.admin.app import _install_template_filters

_TEMPLATES = Path("src/interlock/admin/templates")
DETAIL_PAGES = (
    "pages/identity_detail.html",
    "pages/data_source_detail.html",
    "pages/audit_event_detail.html",
    "pages/write_safety_detail.html",
    "pages/ingestion_job_detail.html",
)


def _source(name: str) -> str:
    return (_TEMPLATES / name).read_text()


def _render(name: str, **ctx: object) -> str:
    """Render with the app's own filters, so a template renders as it ships."""
    templates = Jinja2Templates(directory=str(_TEMPLATES))
    _install_template_filters(templates)
    return templates.env.get_template(name).render(content_only=True, **ctx)


def _identity_context() -> dict[str, object]:
    return {
        "ident": {
            "id": 7,
            "name": "analyst-claude",
            "agent_type": "claude_code",
            "team": "rehearsal",
            "roles": [],
            "source_roles": [{"source_id": "sales_pg", "role": "analyst", "role_id": 3}],
            "mapped_pg_role": None,
            "pg_username": None,
            "api_key_hash": "1348a1dfdd52",
            "enabled": True,
            "created_at": "2026-09-12 23:13:42",
            "updated_at": "2026-09-12 23:13:42",
            "last_used_at": "2026-09-13 01:39:08",
            "rotated_at": None,
        },
        "stats": {"requests_24h": 0, "cache_hit_pct": 0, "p95_ms": 0.0, "denials": 0, "errors": 0},
        "top_sources": [{"source_id": "sales_pg", "cnt": 4}],
        "recent": [],
        "grants": [
            {
                "grant_id": 1,
                "source_id": "sales_pg",
                "source_name": "Sales PostgreSQL",
                "role": "analyst",
                "role_id": 3,
            }
        ],
        "available_roles": [
            {"source_id": "docs_s3", "role_id": 9, "role_key": "reader", "source_name": "Docs S3"}
        ],
        "ident_id": 7,
    }


def test_no_detail_page_still_uses_the_card_grid() -> None:
    """The multi-column grid is the defect: 300px tracks with no min-width reset."""
    offenders = [page for page in DETAIL_PAGES if "detail-grid" in _source(page)]
    assert offenders == []


def test_every_detail_page_stacks_full_width_sections() -> None:
    missing = [page for page in DETAIL_PAGES if "detail-stack" not in _source(page)]
    assert missing == []
    # card-wide existed only to escape the grid.
    assert [page for page in DETAIL_PAGES if "card-wide" in _source(page)] == []


def test_every_detail_table_can_scroll_rather_than_bleed() -> None:
    """A table wider than its section must sit in the scrolling wrapper."""
    for page in DETAIL_PAGES + ("partials/identity_grants.html",):
        source = _source(page)
        tables = source.count("<table")
        wrappers = source.count('class="table-wrapper"')
        assert wrappers >= tables, f"{page}: {tables} table(s), {wrappers} wrapper(s)"


def test_identity_detail_renders_facts_as_a_two_column_table() -> None:
    html = _render("pages/identity_detail.html", **_identity_context())
    assert 'class="form-summary fact-table"' in html
    for label in ("Agent type", "API key hash", "Enabled", "Created"):
        assert label in html


def test_identity_detail_renders_one_metrics_row_with_headers() -> None:
    html = _render("pages/identity_detail.html", **_identity_context())
    assert 'class="metrics-row"' in html
    assert "stat-card" not in html
    metrics = re.search(r'<table class="metrics-row">(.*?)</table>', html, re.S)
    assert metrics is not None
    assert metrics.group(1).count("<th>") == 5  # "<thead>" must not count as a header cell
    assert metrics.group(1).count("<tr") == 2  # one header row, one value row


def test_identity_grants_table_is_wrapped_and_keeps_its_actions() -> None:
    html = _render("partials/identity_grants.html", **_identity_context())
    assert 'class="table-wrapper"' in html
    assert "Revoke" in html and "Grant" in html
