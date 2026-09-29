"""The claim loop must outlive a failure it did not expect.

`_claim_loop` caught only `asyncio.CancelledError`. Anything else - notably
the connection error `_claim_job` raises when the control database goes away -
escaped the `while` and ended claiming for the life of the process. The worker
stayed alive and its container stayed healthy while ingestion was silently
dead, and jobs piled up as `queued` with nothing surfacing the fault.

Measured against the running stack before the fix: a queued job was claimed
within 20s; after a 15 second control-database outage and full recovery, a
queued job sat unclaimed indefinitely and only a container restart brought the
worker back. Nine real jobs had been stranded for nine hours.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from interlock.worker.loop import IngestionWorker


class _Pool:
    """A pool that fails for the first `failures` claims, then serves jobs."""

    def __init__(self, failures: int) -> None:
        self.remaining_failures = failures
        self.claim_attempts = 0

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        self.claim_attempts += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise ConnectionError("control database is unreachable")
        return None


def _loop(pool: Any) -> IngestionWorker:
    worker = IngestionWorker.__new__(IngestionWorker)
    worker._pool = pool
    worker._worker_id = "test-worker"
    worker._running = True
    worker._active_tasks = set()
    worker._concurrency = 1
    worker._poll_interval = 0.01
    # _claim_job reads these too; a bare __new__ leaves them unset, and the
    # resulting AttributeError is caught by the very handler under test - which
    # would make the loop look alive while never reaching the pool at all.
    worker._lease_seconds = 30
    worker._max_lease_reclaims = 3
    return worker


class TestTransientFailures:
    @pytest.mark.asyncio
    async def test_the_loop_keeps_claiming_after_a_database_error(self) -> None:
        """The regression: one connection error used to end claiming forever."""
        pool = _Pool(failures=3)
        worker = _loop(pool)

        task = asyncio.create_task(worker._claim_loop())
        await asyncio.sleep(0.3)
        worker._running = False
        await asyncio.wait_for(task, timeout=2)

        assert pool.claim_attempts > 3, (
            f"the loop stopped after {pool.claim_attempts} attempts; a transient "
            "database failure must not end claiming"
        )
        assert pool.remaining_failures == 0

    @pytest.mark.asyncio
    async def test_a_failing_iteration_backs_off_rather_than_spinning(self) -> None:
        """Retrying without a pause would hammer a database that is already down."""
        pool = _Pool(failures=10_000)
        worker = _loop(pool)
        worker._poll_interval = 0.05

        task = asyncio.create_task(worker._claim_loop())
        await asyncio.sleep(0.3)
        worker._running = False
        await asyncio.wait_for(task, timeout=2)

        # ~6 attempts at a 50ms back-off; a spinning loop would be orders more.
        assert pool.claim_attempts < 40, (
            f"{pool.claim_attempts} attempts in 0.3s - the loop is spinning instead "
            "of backing off"
        )

    @pytest.mark.asyncio
    async def test_cancellation_still_stops_the_loop(self) -> None:
        """Resilience must not swallow the signal that ends a clean shutdown."""
        pool = _Pool(failures=10_000)
        worker = _loop(pool)

        task = asyncio.create_task(worker._claim_loop())
        await asyncio.sleep(0.05)
        task.cancel()

        # Completes rather than hanging: CancelledError is re-raised, not caught
        # by the transient handler.
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
        assert task.done()


def test_the_handler_re_raises_cancellation_explicitly() -> None:
    """Structural, because ordering is what makes this correct.

    A bare `except Exception` placed before the `CancelledError` re-raise would
    still swallow cancellation on Python versions where it derives from
    Exception, and shutdown would hang instead of ending.
    """
    source = inspect.getsource(IngestionWorker._claim_loop)

    assert "except asyncio.CancelledError:\n                    raise" in source, (
        "the claim loop no longer re-raises CancelledError ahead of its general "
        "handler; a clean shutdown could hang"
    )
    assert "except Exception:" in source
