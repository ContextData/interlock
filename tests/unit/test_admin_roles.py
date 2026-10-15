"""Console roles and the user types built from them."""

from __future__ import annotations

import pytest

from interlock.admin.roles import (
    ADMIN_ROLES,
    CUSTOM_ROLES,
    ROLE_MEANINGS,
    USER_TYPES,
    classify_roles,
    is_console_admin,
    is_read_only,
)


def test_every_role_has_a_meaning_and_custom_roles_are_real_roles() -> None:
    assert ADMIN_ROLES == set(ROLE_MEANINGS)
    assert set(CUSTOM_ROLES) <= ADMIN_ROLES
    assert not {"owner", "admin"} & set(CUSTOM_ROLES)
    for _label, roles in USER_TYPES.values():
        assert set(roles) <= ADMIN_ROLES


def test_sso_login_allows_the_same_roles() -> None:
    from interlock.admin.routes.auth import _ADMIN_ROLE_ALLOWLIST

    assert _ADMIN_ROLE_ALLOWLIST == ADMIN_ROLES
    assert "viewer" in _ADMIN_ROLE_ALLOWLIST


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (["owner"], ("administrator", "Administrator")),
        (["admin"], ("administrator", "Administrator")),
        (["Owner", "auditor"], ("administrator", "Administrator")),
        (["security_admin"], ("read_write", "Read-write")),
        (["viewer"], ("read_only", "Read-only")),
        (["approval_reviewer", "auditor"], ("custom", "Custom: approval_reviewer, auditor")),
        (["security_admin", "viewer"], ("custom", "Custom: security_admin, viewer")),
        ([], ("custom", "No roles")),
    ],
)
def test_classify_roles(roles: list[str], expected: tuple[str, str]) -> None:
    assert classify_roles(roles) == expected


def test_read_only_and_console_admin_flags() -> None:
    assert is_read_only(["viewer"])
    assert is_read_only([" Viewer "])
    assert not is_read_only([])
    assert not is_read_only(["viewer", "policy_admin"])
    assert is_console_admin(["owner"]) and is_console_admin(["ADMIN"])
    assert not is_console_admin(["security_admin"])


def _base_template():
    from interlock.admin.app import create_app
    from interlock.config import InterLockConfig

    config = InterLockConfig()
    config.admin.secret_key = "roles-template-test-key-not-used-anywhere"
    return create_app(config=config).state.templates.env.get_template("base.html")


def test_sidebar_labels_read_only_accounts_and_has_a_notice_region() -> None:
    template = _base_template()
    reader = template.render(admin_username="reader", is_read_only=True, active_page="overview")
    operator = template.render(admin_username="op", is_read_only=False, active_page="overview")

    assert "Read-only</span>" in reader
    assert "Read-only</span>" not in operator
    assert 'id="admin-notice"' in operator and 'aria-live="polite"' in operator


def test_render_derives_flags_from_the_session() -> None:
    from types import SimpleNamespace

    from starlette.requests import Request

    from interlock.admin.routes import dashboard

    captured: dict = {}

    class Templates:
        def TemplateResponse(self, request, template, context):  # noqa: N802 - Starlette's name
            captured.update(context)
            return None

    def request_with(admin):
        app = SimpleNamespace(state=SimpleNamespace(templates=Templates()))
        request = Request({"type": "http", "headers": [], "app": app})
        if admin is not None:
            request.state.admin = admin
        return request

    for roles, console_admin, read_only in (
        (("viewer",), False, True),
        (("security_admin",), False, False),
        (("owner",), True, False),
    ):
        captured.clear()
        dashboard._render(request_with(SimpleNamespace(roles=roles, username="u")), "x.html", {})
        assert (captured["is_console_admin"], captured["is_read_only"]) == (console_admin, read_only)

    captured.clear()
    dashboard._render(request_with(None), "x.html", {})
    assert captured["is_console_admin"] is True and captured["is_read_only"] is False
