"""Live certification: gating, credential export, evidence reconciliation.

This module is load-bearing in a way most conftests are not. Credentials must
reach `os.environ` **at import time**, because every live test module gates
itself with a module-level `pytest.mark.skipif`, and those are evaluated when
the module is imported. A fixture would run far too late.

Two independent switches must both be on for anything here to run: the
`live` marker, and `INTERLOCK_LIVE=1`. That mirrors how the e2e suite gates on
`INTERLOCK_E2E`, and it means a stray `-m live` cannot by itself point a test
run at production systems.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.live.support import evidence
from tests.live.support.config import LiveConfig, load_live_config
from tests.live.support.credentials import LiveCredentials, Redactor, load_credentials

# --- import-time credential export ----------------------------------------
# Must precede any test module import. Absent credentials are not an error;
# each module skips on its own predicate.

LIVE_ENABLED = os.environ.get("INTERLOCK_LIVE") == "1"

_CREDENTIALS: LiveCredentials = load_credentials() if LIVE_ENABLED else LiveCredentials()
if LIVE_ENABLED:
    from tests.live.support.credentials import export_environment

    export_environment(_CREDENTIALS)

REDACTOR = Redactor(_CREDENTIALS.secrets)
evidence.configure(REDACTOR)

_SKIP_REASON = "live certification requires INTERLOCK_LIVE=1 and credentials"


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Skip every live test unless both switches are on.

    Belt and braces alongside each module's own skipif: a module that forgets
    its guard still cannot reach a production system by accident.

    The marker is checked with `get_closest_marker`, not `"live" in
    item.keywords`. Keywords include every ancestor node's name, and this
    package is *called* `live`, so the substring form matched everything under
    `tests/live/` - including `test_certification_matrix.py`, which carries no
    marker precisely so it runs in the default suite without credentials.
    Skipping it would have silently disabled the one check that keeps the
    certification report honest when nobody is running live tests.
    """
    if LIVE_ENABLED:
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live_credentials() -> LiveCredentials:
    return _CREDENTIALS


@pytest.fixture(scope="session")
def live_config() -> LiveConfig:
    return load_live_config()


@pytest.fixture(scope="session")
def redactor() -> Redactor:
    return REDACTOR


@pytest.fixture
async def control_plane(live_config: LiveConfig) -> Any:
    """A connection to InterLock's own control database.

    Used to read `audit_log`. This is the one place a live test reads from
    InterLock rather than from an upstream - and legitimately so: the audit
    trail *is* the artifact under test, not a report about something else.
    """
    import asyncpg

    conn = await asyncpg.connect(live_config.e2e.control_dsn)
    try:
        yield conn
    finally:
        await conn.close()


# --- evidence reconciliation ----------------------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Iterator[None]:
    """Downgrade any claimed verdict to the outcome pytest actually observed.

    A test that records PASS and then fails an assertion must not leave a PASS
    in the report. This is what stops `record()` from being another
    self-reported status.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    for entry in evidence.records():
        if entry.nodeid:
            continue
        entry.nodeid = item.nodeid
        if report.failed:
            entry.verdict = evidence.weaker(entry.verdict, evidence.Verdict.FAIL)
        elif report.skipped:
            entry.verdict = evidence.weaker(entry.verdict, evidence.Verdict.UNPROVEN)


def pytest_exception_interact(node: Any, call: Any, report: Any) -> None:
    """Scrub assertion reprs.

    An assertion comparing a redacted response against a raw upstream value
    puts both in the traceback, which makes this the likeliest leak path in a
    live run.
    """
    if report.longrepr is not None:
        report.longrepr = REDACTOR.text(report.longrepr)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if not LIVE_ENABLED or not evidence.records():
        return
    config = load_live_config()
    evidence.write_json(
        Path("build/certification") / f"{config.run_id}-live-certification.json",
        run_id=config.run_id,
        redactor=REDACTOR,
    )
