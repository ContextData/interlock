"""The gateway and admin become ready again after Redis restarts.

On 2026-10-02 the managed Valkey behind the DigitalOcean rehearsal restarted.
The cache-invalidation and FAISS pub/sub listeners caught the dropped
connection, then died in their own cleanup, which sent UNSUBSCRIBE to a server
that was still down. `/ready` reports those listeners, so both services stayed
unready, and served 503, until their pods were restarted by hand about 41
hours later.

This test stops Redis long enough for the listeners to fail and retry, starts
it again, and requires both services to report ready without any restart.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from tests.e2e.support import containers as containers_mod

pytestmark = [pytest.mark.e2e]

# Long enough for each listener to see the dropped connection and fail at
# least one reconnect while Redis is down (the backoff is 1 second).
OUTAGE_SECONDS = 5.0


@pytest.fixture(autouse=True)
def restore_redis() -> Any:
    """Never leave Redis stopped for the tests that follow."""
    try:
        yield
    finally:
        if not containers_mod.is_running(containers_mod.REDIS):
            containers_mod.start(containers_mod.REDIS)


def _ready(url: str) -> int:
    try:
        return httpx.get(f"{url}/ready", timeout=5).status_code
    except httpx.HTTPError:
        return 0


def test_gateway_and_admin_are_ready_again_after_redis_restarts(
    gateway_url: str, admin_url: str
) -> None:
    assert _ready(gateway_url) == 200, "the gateway must start ready"
    assert _ready(admin_url) == 200, "the admin must start ready"

    with containers_mod.stopped(containers_mod.REDIS):
        time.sleep(OUTAGE_SECONDS)
        assert _ready(gateway_url) == 503, "the gateway should notice Redis is gone"

    # Readiness, not liveness: before the fix the processes stayed up and
    # healthy while /ready returned 503 for good.
    containers_mod.wait_until_healthy(f"{gateway_url}/ready", timeout_seconds=60)
    containers_mod.wait_until_healthy(f"{admin_url}/ready", timeout_seconds=60)
