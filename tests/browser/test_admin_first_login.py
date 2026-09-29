"""The first sign-in of a fresh install, in a real browser.

`admin` / `admin` signs in, lands on the change-password page, cannot navigate
away from it, changes the password through the htmx form, and arrives on the
overview. Runs against a second admin in that state, so the shared stack's own
admin is untouched.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import asyncpg
import pytest

from interlock.admin.auth import hash_password
from tests.browser.support import BrowserDiagnostics, capture_evidence, wait_for_htmx
from tests.e2e.support.config import E2EConfig

pytestmark = [pytest.mark.browser, pytest.mark.e2e]


def _run(coro: Any) -> Any:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


async def _execute(config: E2EConfig, query: str, *args: Any) -> Any:
    conn = await asyncpg.connect(config.control_dsn)
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


@pytest.fixture
def default_password_admin(browser_e2e_config: E2EConfig) -> Iterator[str]:
    username = f"browser-first-login-{secrets.token_hex(3)}"
    _run(
        _execute(
            browser_e2e_config,
            """
            INSERT INTO admin_identities
                (username, password_hash, roles, enabled, must_change_password)
            VALUES ($1, $2, ARRAY['owner'], TRUE, TRUE)
            RETURNING id
            """,
            username,
            hash_password("admin"),
        )
    )
    try:
        yield username
    finally:
        _run(
            _execute(
                browser_e2e_config,
                "DELETE FROM admin_identities WHERE username = $1 RETURNING id",
                username,
            )
        )


def test_first_sign_in_forces_a_password_change(
    browser_engine: Any,
    browser_e2e_config: E2EConfig,
    browser_artifact_dir: Any,
    default_password_admin: str,
) -> None:
    base = browser_e2e_config.admin_url
    context = browser_engine.new_context(viewport={"width": 1440, "height": 1000})
    try:
        page = context.new_page()
        diagnostics = BrowserDiagnostics()
        diagnostics.attach(page)

        page.goto(f"{base}/auth/login", wait_until="domcontentloaded")
        page.locator("#lf-user").fill(default_password_admin)
        page.locator("#lf-pass").fill("admin")
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(re.compile(r".*/auth/change-password$"))
        assert "Choose a new password to continue" in page.locator("body").inner_text()
        capture_evidence(page, browser_artifact_dir, "first-login-change-password")

        # Navigating anywhere else comes straight back.
        page.goto(f"{base}/dashboard/data-sources")
        page.wait_for_url(re.compile(r".*/auth/change-password$"))

        new_password = "browser-first-login-" + secrets.token_hex(6)
        page.locator("input[name=current_password]").fill("admin")
        page.locator("input[name=new_password]").fill(new_password)
        page.locator("input[name=confirm_password]").fill(new_password)
        page.get_by_role("button", name="Change password").click()
        page.wait_for_url(re.compile(r".*/dashboard/overview(?:\?.*)?$"))
        wait_for_htmx(page)
        assert page.locator(".sidebar-logo").count() >= 1

        page.goto(f"{base}/dashboard/data-sources")
        assert page.url.endswith("/dashboard/data-sources")
        assert diagnostics.csp_violations == []
        assert diagnostics.page_errors == []
    finally:
        context.close()
