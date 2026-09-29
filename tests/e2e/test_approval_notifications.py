"""A queued write tells a human about itself, without telling them too much.

Before this existed, `ApprovalQueue.submit` inserted a row and raised. Nothing
emitted an event, so the only way to learn that an agent's write was waiting
was to be looking at the Admin console - and the queue expired entries after
fifteen minutes.

The stack certifies both transports at once: the Gateway is configured with
the webhook sender and the Admin with the bot-token sender. That split is also
how an approval's life divides - `pending` and `expired` are emitted by the
Gateway, `approved`, `rejected` and `failed` by the Admin - so one run
exercises both.

Every assertion about a write's effect reads the upstream, never the
approval's status column.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.e2e.support import effects
from tests.e2e.support.clients import mcp_call, wait_for

pytestmark = [pytest.mark.e2e]

MARKER_ID = 1


def _slack_calls(config: Any) -> list[dict[str, Any]]:
    response = httpx.get(f"{config.http_upstream_url}/slack/calls", timeout=10)
    response.raise_for_status()
    calls: list[dict[str, Any]] = response.json()["calls"]
    return calls


def _reset_slack(config: Any) -> None:
    httpx.get(f"{config.http_upstream_url}/slack/reset", timeout=10).raise_for_status()


def _set_slack_failing(config: Any, failing: bool) -> None:
    httpx.get(
        f"{config.http_upstream_url}/slack/fail?on={'1' if failing else '0'}", timeout=10
    ).raise_for_status()


@pytest.fixture
def fake_slack(e2e_config: Any) -> Any:
    _reset_slack(e2e_config)
    yield e2e_config
    _set_slack_failing(e2e_config, False)
    _reset_slack(e2e_config)


def _queue_a_write(e2e_config: Any, note: str) -> int:
    """Issue a MEDIUM-risk write through MCP and return the approval id."""
    response = mcp_call(
        e2e_config,
        "interlock_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": f"UPDATE customers SET note = '{note}' WHERE id = {MARKER_ID}",
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert "Write queued for approval" in body["error"]
    return int(body["approval_id"])


def _calls_for(e2e_config: Any, approval_id: int) -> list[dict[str, Any]]:
    """Only the notifications about one approval.

    Deliberately not positional. Delivery is a background task, so a straggler
    from an earlier test can land after this test's reset, and asserting on
    `calls[-1]` made the suite depend on that timing. Matching the approval id
    is what the test actually means.
    """
    marker = f"#{approval_id}"
    return [call for call in _slack_calls(e2e_config) if marker in call["body"]]


async def _wait_for_event(e2e_config: Any, approval_id: int, phrase: str) -> dict[str, Any]:
    """Wait for one notification about this approval containing `phrase`."""

    async def probe() -> Any:
        for call in _calls_for(e2e_config, approval_id):
            if phrase in json.loads(call["body"])["text"]:
                return call
        return None

    call = await wait_for(probe, timeout_seconds=25)
    assert call, (
        f"no {phrase!r} notification for approval {approval_id}; "
        f"saw {[json.loads(c['body'])['text'] for c in _calls_for(e2e_config, approval_id)]}"
    )
    return dict(call)


async def _note(e2e_config: Any) -> str:
    rows = await effects.mysql_rows(
        e2e_config, f"SELECT note FROM customers WHERE id = {MARKER_ID}"
    )
    return str(rows[0]["note"])


@pytest.mark.asyncio
async def test_a_queued_write_posts_exactly_one_redacted_notification(
    fake_slack: Any, e2e_config: Any
) -> None:
    note = "e2e-notify-secret-value"
    approval_id = _queue_a_write(e2e_config, note)

    call = await _wait_for_event(e2e_config, approval_id, "pending")
    assert (
        len(_calls_for(e2e_config, approval_id)) == 1
    ), f"expected one notification for this approval, got {_calls_for(e2e_config, approval_id)}"

    assert call["path"] == "/slack/webhook"
    payload = json.loads(call["body"])
    assert e2e_config.source_id_mysql in payload["text"]

    # The statement's literal must not leave the system; the shape of the
    # statement must survive, or the message tells a reviewer nothing.
    assert note not in call["body"]
    assert "UPDATE customers" in call["body"]


@pytest.mark.asyncio
async def test_a_slack_outage_never_blocks_the_agent(fake_slack: Any, e2e_config: Any) -> None:
    """The decision is already made and recorded; delivery is best-effort."""
    _set_slack_failing(e2e_config, True)
    before = await _note(e2e_config)

    approval_id = _queue_a_write(e2e_config, "e2e-outage-probe")

    assert approval_id > 0
    # The write is still held, not executed, despite the notifier failing.
    assert await _note(e2e_config) == before


@pytest.mark.asyncio
async def test_approving_executes_upstream_and_posts_the_outcome(
    fake_slack: Any, e2e_config: Any, admin_session: Any
) -> None:
    note = "e2e-approved-value"
    approval_id = _queue_a_write(e2e_config, note)
    await _wait_for_event(e2e_config, approval_id, "pending")

    approved = admin_session.client.post(
        f"/api/approvals/{approval_id}/approve",
        json={},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["executed"] is True

    outcome = await _wait_for_event(e2e_config, approval_id, "approved")
    # The Admin uses the bot transport, so this half also certifies
    # chat.postMessage and its Authorization header.
    assert outcome["path"].endswith("/chat.postMessage")
    assert outcome["authorization"] == "Bearer xoxb-e2e-fake"
    assert note not in outcome["body"]

    assert await _note(e2e_config) == note, "the approved write did not reach the upstream"


@pytest.mark.asyncio
async def test_rejecting_posts_the_outcome_and_never_executes(
    fake_slack: Any, e2e_config: Any, admin_session: Any
) -> None:
    before = await _note(e2e_config)
    approval_id = _queue_a_write(e2e_config, "e2e-rejected-value")
    await _wait_for_event(e2e_config, approval_id, "pending")

    rejected = admin_session.client.post(
        f"/api/approvals/{approval_id}/reject",
        json={},
        headers={"X-CSRF-Token": admin_session.csrf_token},
    )
    assert rejected.status_code == 200, rejected.text

    await _wait_for_event(e2e_config, approval_id, "rejected")

    assert await _note(e2e_config) == before, "a rejected write reached the upstream"
