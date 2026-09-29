"""Render tests for the source-onboarding form partials.

Covers the templates introduced for the New Source / New Identity flow
plus the connection-test result strip. These verify the contract the
dashboard routes rely on (hx-post URLs, named inputs, CSRF target) so
a template-only change cannot silently break the flow.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES = Path("src/interlock/admin/templates")


def _render(template_name: str, **ctx) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template(template_name).render(**ctx)


# ---------------------------------------------------------------------------
# data_source_form.html
# ---------------------------------------------------------------------------


def test_data_source_form_renders_postgresql_fields_by_default() -> None:
    html = _render(
        "partials/data_source_form.html",
        form={
            "source_id": "",
            "name": "",
            "source_type": "postgresql",
            "host": "",
            "port": 5432,
            "database": "",
            "user": "",
            "password": "",
            "base_url": "",
            "cache_strategy": "deterministic_first",
        },
        error=None,
    )
    assert 'name="source_id"' not in html
    assert 'name="source_type"' in html
    assert 'name="host"' in html
    assert 'name="port"' in html
    assert 'hx-post="/dashboard/data-sources/test"' in html
    assert 'hx-post="/dashboard/data-sources/create"' in html


def test_data_source_form_shows_error_banner_when_provided() -> None:
    html = _render(
        "partials/data_source_form.html",
        form={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "host": "h",
            "port": 5432,
            "database": "",
            "user": "",
            "password": "",
            "base_url": "",
            "cache_strategy": "deterministic_first",
        },
        error='A data source with source_id "shop" already exists.',
    )
    assert 'class="form-error"' in html
    assert "already exists" in html


def test_data_source_form_preserves_user_input_on_error() -> None:
    html = _render(
        "partials/data_source_form.html",
        form={
            "source_id": "shop",
            "name": "Shop DB",
            "source_type": "postgresql",
            "host": "db.internal",
            "port": 5433,
            "database": "orders",
            "user": "ro",
            "password": "secret",
            "base_url": "",
            "cache_strategy": "deterministic_first",
        },
        error="boom",
    )
    assert 'value="Shop DB"' in html
    assert 'value="db.internal"' in html
    # password is rendered into the password input - the auto-redaction
    # is done at storage time, the form is allowed to round-trip it.
    assert "secret" in html


# ---------------------------------------------------------------------------
# connection_test_result.html
# ---------------------------------------------------------------------------


def test_connection_test_result_ok_shows_green_dot_and_latency() -> None:
    html = _render(
        "partials/connection_test_result.html",
        ok=True,
        latency_ms=12.4,
        error=None,
    )
    assert "health-green" in html
    assert "12.4 ms" in html


def test_connection_test_result_failure_shows_red_dot_and_error() -> None:
    html = _render(
        "partials/connection_test_result.html",
        ok=False,
        latency_ms=15.0,
        error="connection refused",
    )
    assert "health-red" in html
    assert "connection refused" in html


# ---------------------------------------------------------------------------
# identity_form.html
# ---------------------------------------------------------------------------


def test_identity_form_renders_with_auto_generate_default_on() -> None:
    html = _render(
        "partials/identity_form.html",
        form={
            "name": "",
            "agent_type": "custom",
            "team": "",
            "roles": "analyst,reader",
            "mapped_pg_role": "",
            "generate_key": "on",
            "api_key": "",
        },
        error=None,
        revealed_key=None,
    )
    assert 'name="name"' in html
    assert 'name="generate_key"' in html
    assert "checked" in html  # the auto-generate checkbox is checked
    assert 'hx-post="/dashboard/access-control/identities/create"' in html


def test_identity_form_uses_static_js_for_dynamic_role_grants() -> None:
    source = (_TEMPLATES / "partials/identity_form.html").read_text()
    assert "<script" not in source
    assert "onclick=" not in source
    assert "onchange=" not in source
    assert "roleSelect.innerHTML" not in source
    assert "js-add-grant-row" in source
    assert "js-remove-grant-row" in source


def test_identity_form_has_single_class_attribute_on_cancel() -> None:
    source = (_TEMPLATES / "partials/identity_form.html").read_text()
    assert 'class="btn js-clear-target"' in source
    assert 'data-clear-target="new-identity-form-slot"\n              class=' not in source


def test_data_sources_new_source_opens_modal_target() -> None:
    source = (_TEMPLATES / "pages/data_sources.html").read_text()
    assert 'id="source-modal-root"' in source
    assert 'hx-get="/dashboard/source-wizard"' in source
    assert 'hx-target="#source-modal-root"' in source
    assert 'hx-target="#new-source-form-slot"' not in source


def test_source_wizard_modal_uses_static_js_controls() -> None:
    source = (_TEMPLATES / "partials/source_wizard_modal.html").read_text()
    assert 'role="dialog"' in source
    assert "js-close-modal" in source
    assert "js-modal-backdrop" in source
    assert "onclick=" not in source
    assert "<script" not in source


def test_discovery_tables_are_wrapped_for_mobile_scroll() -> None:
    for name in (
        "partials/discovery_tab.html",
        "pages/discovery_category_detail.html",
        "pages/discovery_entity_detail.html",
        "pages/discovery_asset_detail.html",
    ):
        source = (_TEMPLATES / name).read_text()
        assert "table-wrapper" in source
        assert "js-navigate" not in source


def test_css_defines_used_custom_properties() -> None:
    """Every `var(--x)` anywhere in the console resolves to a definition.

    This used to assert a single literal, `"--muted:" in source`, which
    pinned one token name from the stylesheet that the console rewrite
    replaced. It failed for the right reason - the token is gone - but it
    was never testing what its name says, and a stylesheet can lose a token
    that something still references without `--muted` being involved at all.

    A dangling `var()` is silent in a browser: the property falls back to its
    initial value, so a colour becomes transparent or a spacing becomes zero
    rather than raising anything. That is precisely the failure a rename
    introduces, so the assertion is now over the whole reference graph -
    the CSS, the templates, and the JS that writes inline custom properties.
    """
    css = Path("src/interlock/admin/static/css/dashboard.css").read_text()
    roots = [Path("src/interlock/admin/static/js"), _TEMPLATES]

    defined = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", css))
    used = set(re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)", css))
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".html", ".js"}:
                used |= set(re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)", path.read_text()))

    assert used, "found no custom-property references at all; the scan is broken"
    assert not (used - defined), f"referenced but never defined: {sorted(used - defined)}"


def test_identity_created_partial_displays_one_time_key() -> None:
    html = _render(
        "partials/identity_created.html",
        name="demo-agent",
        api_key="sample-key-not-real",
        roles=["analyst"],
    )
    assert "sample-key-not-real" in html
    assert "shown" in html and "once" in html
    assert "demo-agent" in html


def test_identity_created_copy_button_does_not_inline_api_key_js() -> None:
    source = (_TEMPLATES / "partials/identity_created.html").read_text()
    assert "onclick=" not in source
    assert "navigator.clipboard.writeText('{{ api_key }}')" not in source
    assert "data-copy-text" in source
    assert "js-copy-secret" in source


def test_dashboard_js_builds_role_options_with_dom_apis() -> None:
    js = Path("src/interlock/admin/static/js/dashboard.js").read_text()
    assert "roleSelect.innerHTML" not in js
    assert "document.createElement('option')" in js
    assert "option.textContent = label" in js
    assert "dataset.roleKey" in js


def test_data_source_form_offers_postgresql_tls_defaulting_to_verify_full() -> None:
    html = _render(
        "partials/data_source_form.html",
        form={"source_id": "", "name": "", "source_type": "postgresql", "port": 5432},
        error=None,
    )
    assert 'data-connector="postgresql"' in html
    assert 'name="sslmode"' in html
    assert re.search(r'value="verify-full"\s+selected', html)
    assert 'name="ssl_ca"' in html


def test_data_source_form_hides_tls_fields_for_other_databases() -> None:
    html = _render(
        "partials/data_source_form.html",
        form={"source_id": "", "name": "", "source_type": "mysql", "port": 3306},
        error=None,
    )
    tls_block = re.search(r'<div class="type-block" data-connector="postgresql"[^>]*>', html)
    assert tls_block is not None
    assert "hidden" in tls_block.group(0)
