"""Rate limiting proven against the running gateway.

Phase 6 of the governance audit.

The enforcement branch was already covered by unit tests, but only with a
mocked limiter: they prove the pipeline refuses a request when the limiter
says so, not that a limiter is wired, that Redis is reachable, or that the
configured limit is the one that actually applies. That is the same gap that
let the Write Safety defect through - a mocked collaborator agreeing with the
code under test.

Against the live stack the default limit resolves to 50 requests per 60
seconds per source and identity, and the 51st request is refused. These tests
assert that, and the property that matters more: the limit is scoped to one
identity, so exhausting one agent's quota must not deny service to another.

Each test clears its Redis counters afterwards. A leftover exhausted counter
would fail every later test using that agent for the rest of the window, which
is a real hazard - it happened once while establishing the behaviour by hand.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from tests.e2e.support.clients import http_proxy_request

pytestmark = [pytest.mark.e2e]

# Matches the default in interlock.core.rate_limiter: 50 per 60s per
# (source, identity). Deliberately not imported - if the default changes, this
# should fail and be reconsidered rather than silently track the new value.
_EXPECTED_LIMIT = 50


def _clear_rate_limit_counters() -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "interlock-e2e-redis-1",
            "sh",
            "-c",
            "redis-cli --scan --pattern 'ratelimit:*' | xargs -r redis-cli DEL",
        ],
        capture_output=True,
        check=False,
    )


@pytest.fixture(autouse=True)
def clean_counters() -> Any:
    _clear_rate_limit_counters()
    yield
    _clear_rate_limit_counters()


def _burst(e2e_config: Any, api_key: str, count: int) -> list[int]:
    return [
        http_proxy_request(
            e2e_config,
            "GET",
            "json/customer",
            headers={"Authorization": f"Bearer {api_key}"},
        ).status_code
        for _ in range(count)
    ]


def test_an_identity_over_its_limit_is_refused(e2e_config: Any) -> None:
    """The limiter is wired, reachable, and enforcing the configured number.

    A mocked limiter cannot tell us any of those three things.
    """
    codes = _burst(e2e_config, e2e_config.agent_api_key, _EXPECTED_LIMIT + 10)

    allowed = codes.count(200)
    refused = codes.count(429)
    assert refused > 0, (
        f"no request was rate limited across {len(codes)} in one window: " f"{sorted(set(codes))}"
    )
    assert (
        allowed == _EXPECTED_LIMIT
    ), f"expected exactly {_EXPECTED_LIMIT} allowed before refusal, got {allowed}"
    assert allowed + refused == len(codes), f"unexpected statuses: {sorted(set(codes))}"


def test_the_limit_is_scoped_to_one_identity(e2e_config: Any) -> None:
    """Exhausting one agent must not deny service to another.

    A limiter keyed too broadly turns one noisy agent into an outage for every
    other agent on the source, which is a denial of service the platform would
    be causing itself.
    """
    exhausted = _burst(e2e_config, e2e_config.agent_api_key, _EXPECTED_LIMIT + 5)
    assert 429 in exhausted, "precondition failed: the first agent was never limited"

    other = http_proxy_request(
        e2e_config,
        "GET",
        "json/customer",
        headers={"Authorization": f"Bearer {e2e_config.denied_api_key}"},
    )
    # The second identity is denied by source role, not by rate limit. The
    # distinction is the point: 403 proves it reached the authorization stage
    # rather than being turned away at the limiter on someone else's quota.
    assert other.status_code != 429, (
        "a second identity was rate limited on the first identity's quota -"
        " the limiter is not scoped per identity"
    )


@pytest.mark.asyncio
async def test_a_rate_limited_request_is_audited(e2e_config: Any, control_db: Any) -> None:
    """The 429 must appear in the audit trail, not vanish.

    A refusal that leaves no record is indistinguishable from a request that
    never arrived, which defeats the purpose of an audit trail during an
    incident.
    """
    before = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    codes = _burst(e2e_config, e2e_config.agent_api_key, _EXPECTED_LIMIT + 5)
    assert 429 in codes, "precondition failed: nothing was rate limited"

    import asyncio

    for _ in range(20):
        rows = await control_db.fetch(
            "SELECT id, status, error_message FROM audit_log"
            " WHERE id > $1 AND source_id = $2 AND status = 'denied'"
            " ORDER BY id DESC LIMIT 20",
            before,
            e2e_config.source_id_http,
        )
        if any("rate limit" in str(row["error_message"] or "").lower() for row in rows):
            return
        await asyncio.sleep(0.25)

    pytest.fail("a rate-limited request produced no audit row naming the rate limit")
