"""A readiness failure has to say which condition fired.

`run_dependency_checks` recorded only `type(exc).__name__`, so every failing
check reported "DependencyNotReadyError" and nothing else. For the audit check
that is four different conditions collapsed into one string - a transient
backlog and a permanently wedged one are indistinguishable at the only
endpoint that reports them.

Covered here rather than only end to end because the e2e version has to race a
background flusher: the audit check degrades once a delivery attempt fails, and
that attempt can block on pool acquisition for an unbounded time. These
assertions are on the mechanism itself and are deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from interlock.errors import DependencyNotReadyError
from interlock.gateway.app import _check_audit_buffer
from interlock.observability.health import run_dependency_checks


def _health(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "degraded": False,
        "queue_depth": 0,
        "spool_pending": 0,
        "dlq_count": 0,
        "memory_backlog": 0,
        "last_error": None,
        "partition": None,
        "has_dead_letters": False,
    }
    base.update(overrides)
    health = SimpleNamespace(**base)
    # `degraded` mirrors the real property so a test cannot set an
    # inconsistent pair and prove nothing.
    health.degraded = bool(health.memory_backlog or health.spool_pending or health.last_error)
    health.has_dead_letters = bool(health.dlq_count)
    return health


def _buffer(**overrides: Any) -> SimpleNamespace:
    return SimpleNamespace(health=lambda: _health(**overrides))


class TestAuditCheckReasons:
    def test_a_healthy_buffer_passes_and_reports_its_counters(self) -> None:
        result = _check_audit_buffer(_buffer())  # type: ignore[arg-type]

        assert result["kind"] == "audit"
        assert result["dlq_count"] == 0
        assert result["has_dead_letters"] is False

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"spool_pending": 4}, "4 event(s) pending in the spool"),
            ({"memory_backlog": 2}, "2 event(s) held in memory"),
            ({"last_error": "gaierror"}, "last delivery error: gaierror"),
        ],
    )
    def test_each_condition_names_itself(self, overrides: dict, expected: str) -> None:
        with pytest.raises(DependencyNotReadyError) as excinfo:
            _check_audit_buffer(_buffer(**overrides))  # type: ignore[arg-type]

        assert expected in str(excinfo.value)

    def test_several_conditions_are_all_reported(self) -> None:
        """Reporting only the first would hide the rest during a real incident."""
        with pytest.raises(DependencyNotReadyError) as excinfo:
            _check_audit_buffer(  # type: ignore[arg-type]
                _buffer(spool_pending=3, memory_backlog=1, last_error="boom")
            )

        message = str(excinfo.value)
        assert "pending in the spool" in message
        assert "held in memory" in message
        assert "last delivery error" in message


class TestDeadLettersDoNotGateTraffic:
    def test_dead_letters_alone_leave_the_check_passing(self) -> None:
        """A past durability incident is not a reason to stop serving.

        `_dlq_count` is only incremented and is re-read from the dead-letter
        file at construction, so counting it toward `degraded` made a single
        poison event mark the gateway permanently unready across restarts.
        """
        result = _check_audit_buffer(_buffer(dlq_count=3))  # type: ignore[arg-type]

        assert result["dlq_count"] == 3
        assert result["has_dead_letters"] is True

    def test_dead_letters_are_still_reported_when_something_else_fails(self) -> None:
        """They must stay alertable, not become invisible."""
        with pytest.raises(DependencyNotReadyError):
            _check_audit_buffer(_buffer(dlq_count=3, spool_pending=1))  # type: ignore[arg-type]


class TestReasonReachesThePayload:
    @pytest.mark.asyncio
    async def test_our_own_readiness_errors_carry_their_message(self) -> None:
        def failing() -> dict[str, Any]:
            raise DependencyNotReadyError("audit delivery is degraded: 4 pending")

        ready, results = await run_dependency_checks({"audit": failing}, timeout_seconds=2)

        assert ready is False
        assert results["audit"]["error"] == "DependencyNotReadyError"
        assert results["audit"]["reason"] == "audit delivery is degraded: 4 pending"

    @pytest.mark.asyncio
    async def test_an_arbitrary_exception_is_reduced_to_its_type(self) -> None:
        """Deliberate: an unexpected exception can carry internals.

        A connection error, for instance, routinely contains a DSN. Only our
        own readiness errors have messages written to be shown.
        """
        secret = "postgresql://user:hunter2@db.internal:5432/app"

        def failing() -> dict[str, Any]:
            raise ConnectionError(f"could not connect to {secret}")

        ready, results = await run_dependency_checks({"postgres": failing}, timeout_seconds=2)

        assert ready is False
        assert results["postgres"]["error"] == "ConnectionError"
        assert "reason" not in results["postgres"]
        assert secret not in str(results)

    @pytest.mark.asyncio
    async def test_a_passing_check_keeps_its_detail(self) -> None:
        async def ok() -> dict[str, Any]:
            return {"kind": "audit", "queue_depth": 0}

        ready, results = await run_dependency_checks({"audit": ok}, timeout_seconds=2)

        assert ready is True
        assert results["audit"]["status"] == "ok"
        assert results["audit"]["kind"] == "audit"
