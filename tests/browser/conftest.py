"""Opt-in Playwright fixtures backed by the deterministic E2E Compose stack."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from interlock.admin.auth import hash_password
from tests.browser.support import BROWSER_ADMIN_PASSWORD, BrowserDiagnostics, login_admin
from tests.e2e.support.config import E2EConfig, load_e2e_config
from tests.e2e.support.seed import seed_all

BROWSER_ENABLED = (
    os.environ.get("INTERLOCK_E2E", "0") == "1" and os.environ.get("INTERLOCK_BROWSER", "0") == "1"
)
BROWSER_ADMINS = {
    "browser-auditor": ("auditor",),
    "browser-source-admin": ("source_admin",),
    "browser-policy-admin": ("policy_admin",),
    "browser-approval-reviewer": ("approval_reviewer",),
}


def _run_async(coro: Any) -> Any:
    """Run async DB setup away from Playwright/pytest's active event loop."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if BROWSER_ENABLED:
        return
    marker = pytest.mark.skip(
        reason="Set INTERLOCK_E2E=1 and INTERLOCK_BROWSER=1 to run browser certification"
    )
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(marker)


@pytest.fixture(scope="session")
def browser_e2e_config() -> E2EConfig:
    return load_e2e_config()


@pytest.fixture(scope="session", autouse=True)
def seeded_browser_stack(browser_e2e_config: E2EConfig) -> Iterator[None]:
    if not BROWSER_ENABLED:
        yield
        return
    _run_async(seed_all(browser_e2e_config))
    _run_async(_seed_browser_admins(browser_e2e_config))
    yield


async def _seed_browser_admins(config: E2EConfig) -> None:
    connection = await asyncpg.connect(config.control_dsn)
    try:
        password_hash = hash_password(BROWSER_ADMIN_PASSWORD)
        for username, roles in BROWSER_ADMINS.items():
            await connection.execute(
                """
                INSERT INTO admin_identities (username, password_hash, roles, enabled)
                VALUES ($1, $2, $3, TRUE)
                ON CONFLICT (username) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    roles = EXCLUDED.roles,
                    enabled = TRUE,
                    updated_at = NOW()
                """,
                username,
                password_hash,
                list(roles),
            )
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def browser_engine() -> Iterator[Any]:
    if not BROWSER_ENABLED:
        pytest.skip("Browser certification is disabled")
    try:
        import playwright.sync_api as playwright_module
    except ImportError:
        pytest.fail("Playwright is required when browser certification is enabled")
    manager = playwright_module.sync_playwright().start()
    try:
        try:
            browser = manager.chromium.launch(
                headless=os.environ.get("INTERLOCK_BROWSER_HEADLESS", "1") != "0"
            )
        except Exception as exc:
            pytest.fail(f"Playwright Chromium is unavailable: {exc}")
        try:
            yield browser
        finally:
            browser.close()
    finally:
        manager.stop()


@pytest.fixture
def admin_page_factory(
    browser_engine: Any,
    browser_e2e_config: E2EConfig,
) -> Iterator[Callable[..., tuple[Any, Any, BrowserDiagnostics]]]:
    contexts: list[Any] = []

    def factory(
        *,
        username: str | None = None,
        password: str | None = None,
        viewport: dict[str, int] | None = None,
    ) -> tuple[Any, Any, BrowserDiagnostics]:
        context = browser_engine.new_context(
            viewport=viewport or {"width": 1440, "height": 1000},
            reduced_motion="reduce",
        )
        contexts.append(context)
        page = context.new_page()
        diagnostics = BrowserDiagnostics()
        diagnostics.attach(page)
        login_admin(
            page,
            browser_e2e_config.admin_url,
            username or browser_e2e_config.admin_username,
            password or browser_e2e_config.admin_password,
        )
        diagnostics.reset()
        return context, page, diagnostics

    yield factory
    for context in reversed(contexts):
        context.close()


@pytest.fixture(scope="session")
def browser_artifact_dir() -> Path | None:
    value = os.environ.get("INTERLOCK_BROWSER_ARTIFACT_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else None


@pytest.fixture
def seeded_browser_approval(browser_e2e_config: E2EConfig) -> Iterator[tuple[int, str]]:
    marker = "browser-approval-secret-value"
    approval_id = _run_async(_insert_browser_approval(browser_e2e_config, marker))
    try:
        yield approval_id, marker
    finally:
        _run_async(_delete_browser_approval(browser_e2e_config, approval_id))


async def _insert_browser_approval(config: E2EConfig, marker: str) -> int:
    connection = await asyncpg.connect(config.control_dsn)
    try:
        identity_id = await connection.fetchval(
            "SELECT id FROM identities WHERE enabled = TRUE ORDER BY id LIMIT 1"
        )
        assert identity_id is not None, "E2E seed did not create an enabled identity"
        return int(
            await connection.fetchval(
                """
                INSERT INTO write_approval_queue
                    (identity_id, source_id, sql_text, risk_level, status,
                     expires_at, request_metadata)
                VALUES ($1, $2, $3, 'high', 'pending', NOW() + INTERVAL '30 minutes', $4::jsonb)
                RETURNING id
                """,
                identity_id,
                config.source_id_pg,
                f"DELETE FROM customers WHERE email = '{marker}'",
                json.dumps(
                    {
                        "authorization": f"Bearer {marker}",
                        "reason": "Browser certification approval",
                    }
                ),
            )
        )
    finally:
        await connection.close()


async def _delete_browser_approval(config: E2EConfig, approval_id: int) -> None:
    connection = await asyncpg.connect(config.control_dsn)
    try:
        await connection.execute("DELETE FROM write_approval_queue WHERE id = $1", approval_id)
    finally:
        await connection.close()


@pytest.fixture
def seeded_browser_identity_id(browser_e2e_config: E2EConfig) -> int:
    """The seeded agent identity, for the identity detail route."""
    return int(_run_async(_first_identity_id(browser_e2e_config)))


async def _first_identity_id(config: E2EConfig) -> int:
    connection = await asyncpg.connect(config.control_dsn)
    try:
        identity_id = await connection.fetchval(
            "SELECT id FROM identities WHERE enabled = TRUE ORDER BY id LIMIT 1"
        )
        assert identity_id is not None, "E2E seed did not create an enabled identity"
        return int(identity_id)
    finally:
        await connection.close()


@pytest.fixture
def seeded_browser_ingestion_job(browser_e2e_config: E2EConfig) -> Iterator[int]:
    job_id = _run_async(_insert_browser_ingestion_job(browser_e2e_config))
    try:
        yield job_id
    finally:
        _run_async(_delete_browser_ingestion_job(browser_e2e_config, job_id))


async def _insert_browser_ingestion_job(config: E2EConfig) -> int:
    connection = await asyncpg.connect(config.control_dsn)
    try:
        return int(
            await connection.fetchval(
                """
                INSERT INTO ingestion_jobs (source_id, file_path, status, error_message, metadata)
                VALUES ($1, $2, 'failed', 'browser certification fixture', $3::jsonb)
                ON CONFLICT (source_id, file_path) DO UPDATE SET status = EXCLUDED.status
                RETURNING id
                """,
                config.source_id_pg,
                "/browser-certification/fixture.md",
                json.dumps({"note": "browser certification"}),
            )
        )
    finally:
        await connection.close()


async def _delete_browser_ingestion_job(config: E2EConfig, job_id: int) -> None:
    connection = await asyncpg.connect(config.control_dsn)
    try:
        await connection.execute("DELETE FROM ingestion_jobs WHERE id = $1", job_id)
    finally:
        await connection.close()


@pytest.fixture
def seeded_browser_audit(browser_e2e_config: E2EConfig) -> Iterator[tuple[int, str]]:
    marker = "browser-audit-secret-value"
    audit_id = _run_async(_insert_browser_audit(browser_e2e_config, marker))
    try:
        yield audit_id, marker
    finally:
        _run_async(_delete_browser_audit(browser_e2e_config, audit_id))


async def _insert_browser_audit(config: E2EConfig, marker: str) -> int:
    connection = await asyncpg.connect(config.control_dsn)
    try:
        identity_id = await connection.fetchval(
            "SELECT id FROM identities WHERE enabled = TRUE ORDER BY id LIMIT 1"
        )
        return int(
            await connection.fetchval(
                """
                INSERT INTO audit_log
                    (identity_id, source_id, operation, status, protocol, route,
                     normalized_operation, request_metadata)
                VALUES ($1, $2, 'browser.certification', 'success', 'admin',
                        '/browser-certification', 'browser.certification', $3::jsonb)
                RETURNING id
                """,
                identity_id,
                config.source_id_pg,
                json.dumps(
                    {
                        "authorization": f"Bearer {marker}",
                        "note": f"token={marker}",
                    }
                ),
            )
        )
    finally:
        await connection.close()


async def _delete_browser_audit(config: E2EConfig, audit_id: int) -> None:
    connection = await asyncpg.connect(config.control_dsn)
    try:
        await connection.execute("DELETE FROM audit_log WHERE id = $1", audit_id)
    finally:
        await connection.close()
