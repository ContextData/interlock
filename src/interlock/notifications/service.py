"""Fire-and-forget delivery that cannot affect the request that triggered it.

`notify()` is synchronous and returns after scheduling. Everything that can
fail - the network, Slack, a bad channel - happens on a background task whose
exceptions are logged and dropped. A governance decision has already been made
and recorded by the time this runs; a notification failure must not turn into
an agent-visible error or a slower request.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from interlock.notifications.events import ApprovalEvent
from interlock.notifications.messages import build_slack_payload
from interlock.notifications.slack import SlackSender

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 5.0


class ApprovalNotifierProtocol(Protocol):
    """What `ApprovalQueue` depends on. Deliberately one synchronous method."""

    def notify(self, event: ApprovalEvent) -> None: ...


class ApprovalNotifier:
    def __init__(
        self,
        sender: SlackSender,
        *,
        events: tuple[str, ...] | list[str],
        timeout_seconds: float = 5.0,
        max_attempts: int = 3,
        admin_base_url: str | None = None,
    ) -> None:
        self._sender = sender
        self._events = set(events)
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._admin_base_url = admin_base_url
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending_count(self) -> int:
        return len(self._tasks)

    def notify(self, event: ApprovalEvent) -> None:
        """Schedule delivery. Never raises, never awaits."""
        if event.kind not in self._events:
            return
        try:
            task = asyncio.create_task(self._deliver(event))
        except RuntimeError:
            # No running loop, e.g. a synchronous test harness. Dropping the
            # notification is correct; failing the caller is not.
            logger.warning(
                "approval notification for %d not scheduled: no running event loop",
                event.approval_id,
            )
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, event: ApprovalEvent) -> None:
        payload = build_slack_payload(event, admin_base_url=self._admin_base_url)
        delay = 0.5
        for attempt in range(1, self._max_attempts + 1):
            try:
                await asyncio.wait_for(self._sender.send(payload), timeout=self._timeout)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Type name only: an exception message can carry the webhook
                # URL or a Slack token.
                logger.warning(
                    "approval notification %s for %d failed (attempt %d/%d): %s",
                    event.kind,
                    event.approval_id,
                    attempt,
                    self._max_attempts,
                    type(exc).__name__,
                )
            if attempt < self._max_attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
        logger.warning(
            "approval notification %s for %d gave up after %d attempts",
            event.kind,
            event.approval_id,
            self._max_attempts,
        )

    async def aclose(self, *, drain_seconds: float = 5.0) -> None:
        """Wait briefly for in-flight deliveries, then give up on them."""
        if not self._tasks:
            return
        pending = list(self._tasks)
        done, still_running = await asyncio.wait(pending, timeout=drain_seconds)
        del done
        for task in still_running:
            task.cancel()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"ApprovalNotifier(sender={type(self._sender).__name__}, events={sorted(self._events)})"
        )
