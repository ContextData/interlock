"""Audit durability under a real control-plane outage.

Phase 2 of the governance audit's remaining work, and the first tests in this
suite to stop a container rather than merely talk to one.

The guarantee worth proving is that no governed action goes unrecorded. Two
things about how that is asserted here.

**The guarantee, not the mechanism.** An event that cannot be delivered may sit
in the in-memory queue or be written to the fsynced spool, depending on where
the flush cycle happened to be when the database went away. Both satisfy the
guarantee; asserting on spool line counts would test an implementation detail
and would be flaky by construction. So these tests count audit rows before and
after, and require the arithmetic to work out.

**The failure is vacuous, and saying so is the point.** With the control
database stopped, every governed request is refused at authentication -
`AuthManager.authenticate` revalidates against PostgreSQL on every request, and
`SourceRoleEvaluator.evaluate` does an uncached fetch. So no action reaches an
upstream while the audit trail is unavailable: the system fails closed *before*
the thing it would have needed to record. That is a good property and the
strongest form of the guarantee actually available, but it is not the dramatic
one ("the write succeeded and was still audited"), and a report that implied
otherwise would be overclaiming.

The stack runs `durability_mode=strict` for these tests - the mode production
requires. It previously ran `retriable`, in which `AuditBuffer.log()` can never
refuse, so every fail-closed behaviour was unreachable end to end.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from tests.e2e.support import containers as containers_mod
from tests.e2e.support.config import E2EConfig
from tests.e2e.support.effects import http_upstream_calls

pytestmark = [pytest.mark.e2e, pytest.mark.durability]

SPOOL = "/var/lib/interlock/audit-spool/audit-spool.jsonl"
DLQ = "/var/lib/interlock/audit-spool/audit-dead-letter.jsonl"


def _request(config: E2EConfig) -> int | str:
    """One governed request. Returns a status code, or the error class name."""
    try:
        return httpx.get(
            f"{config.gateway_url}/proxy/{config.source_id_http}/json/customer",
            headers={"Authorization": f"Bearer {config.agent_api_key}"},
            timeout=10,
        ).status_code
    except Exception as exc:  # noqa: BLE001 - a dropped connection is a valid outcome
        return type(exc).__name__


def _readiness(config: E2EConfig) -> tuple[int, dict[str, Any]]:
    response = httpx.get(f"{config.gateway_url}/ready", timeout=5)
    checks = response.json().get("checks", {}) if response.content else {}
    return response.status_code, checks.get("audit", {})


async def _audit_totals_fresh(config: E2EConfig) -> tuple[int, int]:
    """Open a new connection to count. See the note at the call site."""
    import asyncpg

    conn = await asyncpg.connect(config.control_dsn)
    try:
        return await _audit_totals(conn)
    finally:
        await conn.close()


async def _audit_totals(control_db: Any) -> tuple[int, int]:
    """(rows, distinct event ids). Their difference is the duplicate count."""
    total = int(await control_db.fetchval("SELECT count(*) FROM audit_log"))
    distinct = int(await control_db.fetchval("SELECT count(DISTINCT event_id) FROM audit_log"))
    return total, distinct


@pytest.fixture(scope="module", autouse=True)
def settled_gateway(e2e_config: E2EConfig) -> Any:
    """Do not start measuring against a gateway that is still coming up.

    These tests take a baseline audit-row count, cause an outage, and require
    the arithmetic to work out afterwards. A gateway whose audit buffer has
    not finished establishing its pool has events in flight that land after
    the baseline is taken, which showed up once as a failure in the run
    immediately following a container recreate - a flake caused by the harness
    rather than by the system.
    """
    containers_mod.wait_until_healthy(f"{e2e_config.gateway_url}/health")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{e2e_config.gateway_url}/ready", timeout=5).status_code == 200:
                break
        except Exception:  # noqa: BLE001 - keep waiting through a starting gateway
            pass
        time.sleep(1)
    # Let anything already queued drain, so the baseline is a quiet system.
    time.sleep(3)
    return None


@pytest.fixture(autouse=True)
def restore_control_plane() -> Any:
    """Put the control database back whatever the test did.

    `make e2e` does not bring the stack up between files, so a control
    database left stopped would fail every test that follows - and the failure
    would look like the system rather than like this fixture.
    """
    try:
        yield
    finally:
        if not containers_mod.is_running(containers_mod.CONTROL_POSTGRES):
            containers_mod.start(containers_mod.CONTROL_POSTGRES)
        containers_mod.wait_for_postgres()


def test_the_stack_under_test_runs_the_mode_production_requires() -> None:
    """Otherwise these tests certify a configuration nobody ships.

    `retriable` is the one mode in which the audit buffer can never refuse a
    request, so every fail-closed assertion below would be unreachable.
    """
    mode = containers_mod.exec_in(
        containers_mod.GATEWAY,
        "python",
        "-c",
        "import os; print(os.environ.get('INTERLOCK_AUDIT__DURABILITY_MODE', ''))",
    ).strip()

    assert mode == "strict", (
        f"the gateway is running durability_mode={mode!r}; production requires "
        "'strict' and these guarantees are only meaningful in it"
    )


@pytest.mark.asyncio
async def test_no_action_reaches_an_upstream_while_the_audit_trail_is_down(
    e2e_config: E2EConfig, http_upstream: Any
) -> None:
    """Fail closed *before* the action, asserted against the origin's own log.

    Read this as the vacuous form of the guarantee and no more: the system
    refuses rather than acting unrecorded. Concluding from the refusal alone
    would be trusting the gateway's report, so the origin's call log is what
    is actually checked.
    """
    httpx.post(f"{e2e_config.http_upstream_url}/reset", timeout=10)
    # Count only the governed path. The mock origin records every request it
    # receives *including* the /calls read used to inspect it, so an unfiltered
    # count grows by one each time it is measured - the probe polluting its own
    # measurement.
    before = len(http_upstream_calls(e2e_config, path="/json/customer"))

    with containers_mod.stopped(containers_mod.CONTROL_POSTGRES):
        time.sleep(2)
        codes = [_request(e2e_config) for _ in range(6)]

    after = len(http_upstream_calls(e2e_config, path="/json/customer"))

    assert after == before, (
        f"the origin was contacted {after - before} time(s) while the control "
        "database was down, so an action occurred that could not be audited"
    )
    assert all(
        code != 200 for code in codes
    ), f"a governed request succeeded during the outage: {codes}"


@pytest.mark.asyncio
async def test_every_request_during_an_outage_is_audited_exactly_once_afterwards(
    e2e_config: E2EConfig,
) -> None:
    """The durability guarantee, and the idempotency that makes it safe.

    Counted rather than inspected: whether an event waited in the queue or in
    the spool depends on where the flush cycle was, and both are correct. What
    must hold is that the arithmetic works out and nothing is written twice -
    replay claims events through `audit_event_dedup`, so a double replay must
    not double-count.
    """
    # A fresh connection per read: the session-scoped one does not survive its
    # server being stopped, and reusing it here fails with "connection is
    # closed" - which would look like a durability failure rather than a
    # broken fixture.
    before_total, _ = await _audit_totals_fresh(e2e_config)
    attempts = 12

    with containers_mod.stopped(containers_mod.CONTROL_POSTGRES):
        time.sleep(2)
        for _ in range(attempts):
            _request(e2e_config)
        time.sleep(4)

    # Drive a little traffic while waiting: the flusher only clears its error
    # state on a successful batch, so a purely idle wait proves less.
    for _ in range(30):
        if containers_mod.count_lines(containers_mod.GATEWAY, SPOOL) == 0:
            break
        _request(e2e_config)
        await asyncio.sleep(1)

    await asyncio.sleep(3)
    after_total, after_distinct = await _audit_totals_fresh(e2e_config)

    assert after_total - before_total >= attempts, (
        f"{attempts} requests were made during the outage but only "
        f"{after_total - before_total} audit rows appeared: events were lost"
    )
    assert (
        after_total == after_distinct
    ), f"{after_total - after_distinct} duplicate audit row(s): replay is not idempotent"
    assert (
        containers_mod.count_lines(containers_mod.GATEWAY, SPOOL) == 0
    ), "the spool still holds events after recovery"


@pytest.mark.asyncio
async def test_readiness_recovers_after_the_outage(e2e_config: E2EConfig) -> None:
    """A gateway that stays unready after its dependency returns is an outage of its own."""
    with containers_mod.stopped(containers_mod.CONTROL_POSTGRES):
        time.sleep(2)
        for _ in range(4):
            _request(e2e_config)
        status, _audit = _readiness(e2e_config)
        assert status == 503, "readiness stayed green while the control database was down"

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, _audit = _readiness(e2e_config)
        if status == 200:
            return
        _request(e2e_config)
        await asyncio.sleep(1)

    pytest.fail("readiness never recovered after the control database returned")


def test_a_degraded_readiness_check_says_which_condition_fired(
    e2e_config: E2EConfig,
) -> None:
    """A failing check must name its condition, not just its exception class.

    The payload used to carry only `type(exc).__name__`, so every failure read
    "DependencyNotReadyError" and an operator could not tell which dependency
    had failed or why.

    Asserted against *whichever* checks fail, deliberately. An earlier version
    waited for the audit check specifically and was intermittent: the audit
    check only degrades once the flusher has attempted a delivery, and that
    attempt can block on pool acquisition for an unbounded time. Racing a
    background task is not something a test should do, and the mechanism being
    verified here - that a readiness reason reaches the payload - is the same
    whichever check reports it. The audit check's own reason text is pinned
    deterministically in tests/unit/test_readiness_reasons.py.
    """
    with containers_mod.stopped(containers_mod.CONTROL_POSTGRES):
        time.sleep(3)
        for _ in range(4):
            _request(e2e_config)
        response = httpx.get(f"{e2e_config.gateway_url}/ready", timeout=5)
        checks = response.json().get("checks", {})

    assert response.status_code == 503, "readiness stayed green with the control database down"

    failing = {name: detail for name, detail in checks.items() if detail.get("status") == "error"}
    assert failing, f"/ready returned 503 with no failing check: {checks}"

    with_reasons = {name: detail.get("reason") for name, detail in failing.items()}
    assert any(with_reasons.values()), (
        "no failing check carried a reason, so the payload says only that "
        f"something is wrong: {with_reasons}"
    )


def test_a_past_dead_letter_does_not_remove_a_healthy_gateway_from_service(
    e2e_config: E2EConfig,
) -> None:
    """A durability incident is worth alerting on; it is not a reason to stop serving.

    `_dlq_count` is only ever incremented, and is re-read from the dead-letter
    file at construction. While it counted toward `degraded`, a single poison
    event marked the gateway permanently unready - across restarts, because the
    file lives on the spool volume. In Kubernetes that removes a healthy pod
    and never puts it back.

    Demonstrated before the fix: one line appended to that file made an
    otherwise perfectly healthy gateway answer 503 forever, including after
    successful traffic.
    """
    line = '{"event":{"raw_spool_line":"synthetic"},"attempts":1,"error_type":"DataError"}'
    containers_mod.exec_in(containers_mod.GATEWAY, "sh", "-c", f"printf '%s\\n' '{line}' >> {DLQ}")
    try:
        containers_mod.restart(containers_mod.GATEWAY)
        containers_mod.wait_until_healthy(f"{e2e_config.gateway_url}/health")
        time.sleep(4)

        status, audit = _readiness(e2e_config)

        assert status == 200, (
            "a single historical dead letter made a healthy gateway unready; "
            f"audit check: {audit}"
        )
        assert audit.get("dlq_count", 0) >= 1, (
            "the dead letter is no longer reported, so it cannot be alerted on - "
            "it must stay visible even though it does not gate traffic"
        )
        assert audit.get("has_dead_letters") is True
    finally:
        containers_mod.exec_in(containers_mod.GATEWAY, "sh", "-c", f"rm -f {DLQ}")
        containers_mod.restart(containers_mod.GATEWAY)
        containers_mod.wait_until_healthy(f"{e2e_config.gateway_url}/health")
