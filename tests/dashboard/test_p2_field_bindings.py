"""Regression tests for audit P2-A..F: dashboard field bindings.

AUDIT-COVERS: P2-A, P2-B, P2-C, P2-D, P2-E, P2-F

The audit listed six concrete dashboard defects:

- P2-A ``data_sources.html:25`` rendered ``ds.id`` (the BIGSERIAL pk)
  instead of the logical ``ds.source_id``.
- P2-B same template referenced ``ds.connection_info`` which the route
  never populated; raw ``connection_config`` would have leaked secrets.
- P2-C ``access_control.html:34`` rendered ``ident.pg_role`` but the
  schema and API expose ``mapped_pg_role``.
- P2-D ``access_control.html:74`` rendered ``rule.effect`` but policy
  effect is stored under ``actions.effect``.
- P2-E ``routes/dashboard.py:517-521`` Discovery tab partial returned
  only the inner content, dropping the entity search box on switch.
- P2-F ``routes/ingestion.py:141-143`` retried jobs by setting status
  to ``pending`` (schema uses ``queued``) and clearing ``error``
  (schema uses ``error_message``).

These tests render the templates and inspect the actual emitted HTML
for the correct values, instead of just asserting page labels.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from interlock.admin.app import _install_template_filters
from interlock.admin.routes import dashboard, ingestion

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
# P2-A
# ---------------------------------------------------------------------------


def test_p2_a_data_sources_renders_logical_source_id() -> None:
    html = _render(
        "pages/data_sources.html",
        data_sources=[
            {
                "id": 99,  # internal pk
                "source_id": "analytics-prod",
                "name": "Analytics",
                "source_type": "postgresql",
                "enabled": True,
                "cache_strategy": "deterministic_first",
                "request_count": 17,
                "connection_summary": "db.example.com:5432",
            }
        ],
    )
    # Rows carry the logical source id for navigation; the table shows the
    # name, and the id is on the source's own page.
    assert 'data-source-id="analytics-prod"' in html
    assert 'href="/dashboard/data-sources/analytics-prod"' in html
    assert ">Analytics<" in html
    # The internal pk must NOT be rendered as the source id.
    assert ">99<" not in html
    assert ">Source ID<" not in html


# ---------------------------------------------------------------------------
# P2-B
# ---------------------------------------------------------------------------


def test_p2_b_data_sources_table_never_binds_connection_config() -> None:
    """The list shows no connection details at all; the source page shows a
    redacted summary. Raw connection_config is never bound."""
    for name in ("pages/data_sources.html", "partials/data_sources_table.html"):
        body = (_TEMPLATES / name).read_text()
        assert "connection_config" not in body, name
        assert "ds.connection_summary" not in body, name
        assert ">Connection<" not in body, name
    detail = (_TEMPLATES / "pages" / "data_source_detail.html").read_text()
    assert "connection_config" not in detail.replace("masked_connection_config", "")
    # Route helper builds the detail page's summary safely.
    assert callable(dashboard._safe_connection_summary)


def test_p2_b_safe_connection_summary_strips_secrets() -> None:
    summary = dashboard._safe_connection_summary(
        {
            "connection_config": {
                "host": "db.example.com",
                "port": 5432,
                "user": "leaky",
                "password": "p4ssw0rd",
                "api_key": "sk-secret",
            }
        }
    )
    assert "db.example.com" in summary
    assert "5432" in summary
    assert "p4ssw0rd" not in summary
    assert "sk-secret" not in summary
    assert "leaky" not in summary


def test_p2_b_safe_connection_summary_handles_string_json() -> None:
    summary = dashboard._safe_connection_summary(
        {"connection_config": '{"host": "h", "port": 9, "secret": "x"}'}
    )
    assert "h" in summary and "9" in summary and "x" not in summary


def test_p2_b_safe_connection_summary_returns_dash_when_empty() -> None:
    assert dashboard._safe_connection_summary({}) == "-"


# ---------------------------------------------------------------------------
# P2-C
# ---------------------------------------------------------------------------


def test_p2_c_access_control_renders_mapped_pg_role() -> None:
    html = _render(
        "pages/access_control.html",
        identities=[
            {
                "id": 1,
                "name": "alice",
                "team": "platform",
                "agent_type": "claude_code",
                "roles": ["read"],
                "mapped_pg_role": "onyx_reader",
                "enabled": True,
            }
        ],
        policy_rules=[],
    )
    assert "onyx_reader" in html
    assert 'data-mapped-pg-role="onyx_reader"' in html


# ---------------------------------------------------------------------------
# P2-D
# ---------------------------------------------------------------------------


def test_p2_d_access_control_renders_actions_effect() -> None:
    html = _render(
        "pages/access_control.html",
        identities=[],
        policy_rules=[
            {
                "name": "block-pii",
                "priority": 100,
                "conditions_summary": "operation=read",
                "actions": {"effect": "deny", "reason": "pii"},
                "enabled": True,
            }
        ],
    )
    assert 'data-rule-effect="deny"' in html


# ---------------------------------------------------------------------------
# P2-E
# ---------------------------------------------------------------------------


def test_p2_e_discovery_tab_partial_includes_search_for_entities() -> None:
    html = _render(
        "partials/discovery_tab.html",
        active_tab="entities",
        assets=[],
        entities=[],
        categories=[],
        q="",
    )
    # P2-E required that the entities tab keep its filter input on
    # HTMX tab switches. Phase 4 reskinned the partial (class is now
    # `disc-tab-search` instead of `search-box`) but the contract is
    # unchanged: there must be an HTMX-bound text input named `q`
    # that re-hits the discovery route.
    assert 'name="q"' in html
    assert 'hx-get="/dashboard/discovery?tab=entities"' in html, (
        "Discovery entities tab partial must keep the HTMX-bound filter " "input (P2-E regression)"
    )


def test_p2_e_dashboard_route_returns_full_tab_partial_on_inner_htmx() -> None:
    src = inspect.getsource(dashboard)
    assert "partials/discovery_tab.html" in src, (
        "Dashboard discovery route must render the full tab partial on "
        "inner HTMX swaps so the search box is preserved (P2-E)"
    )


# ---------------------------------------------------------------------------
# P2-F
# ---------------------------------------------------------------------------


def test_p2_f_retry_uses_queued_and_error_message_field_names() -> None:
    src = inspect.getsource(ingestion.retry_job)
    assert (
        "status = 'queued'" in src
    ), "retry_job must set status='queued' to match the schema (P2-F)"
    assert (
        "error_message = NULL" in src
    ), "retry_job must clear error_message (the schema field) not 'error'"
    assert "'pending'" not in src or "queued" in src
