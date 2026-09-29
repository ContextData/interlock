"""Tier 0: the live Slack workspace, before InterLock is involved.

Read this module's scope carefully, because it is easy to overstate.

Everything here goes through slack-sdk **directly**. None of it exercises
InterLock. That is not a shortcut: `SlackAdapter.execute_write` raises
`NotImplementedError("Slack writes are intentionally disabled for the Tier 1
MVP adapter")`, so a governed Slack write does not exist to certify. What this
module proves is that the bot token is real, carries the scopes the connector
needs, and can reach the configured channel - the precondition for the
governed *read* tests, and nothing more.

The certification report must describe it in those terms. A reader who came
away believing InterLock governed a Slack write would have been misled by the
report, not by the product.

Every message is prefixed with the run marker and deleted in teardown. The
posts are briefly visible to anyone in the channel, which the owner approved.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

from tests.live.support import effects
from tests.live.support.config import LiveConfig, load_live_config
from tests.live.support.evidence import Control, Verdict, record

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not load_live_config().has_slack(),
        reason="live Slack credentials are not configured",
    ),
]


@pytest.fixture(scope="module")
def posted(live_config: LiveConfig) -> Iterator[list[str]]:
    """Track every message this module posts, and delete them all afterwards.

    Teardown runs in `finally` and swallows per-message failures: a cleanup
    error must not mask the test failure that preceded it. Anything it cannot
    remove stays findable by the run marker.
    """
    timestamps: list[str] = []
    try:
        yield timestamps
    finally:
        for ts in timestamps:
            try:
                effects.slack_delete(live_config, ts)
            except Exception as exc:  # noqa: BLE001 - warned about, never raised
                warnings.warn(
                    f"could not delete Slack message {ts} "
                    f"({type(exc).__name__}); find it by the marker "
                    f"{live_config.slack_marker!r}",
                    stacklevel=1,
                )


def test_the_bot_token_authenticates(live_config: LiveConfig) -> None:
    from slack_sdk import WebClient

    identity = WebClient(token=live_config.slack_bot_token).auth_test()

    record(
        "slack",
        Control.UPSTREAM,
        Verdict.PASS,
        detail="bot token authenticated against the workspace",
        team=identity.get("team"),
        bot_user=identity.get("user"),
    )
    assert identity.get("ok"), "auth.test did not succeed"


def test_the_configured_channel_is_readable(live_config: LiveConfig) -> None:
    """Read scope on the target channel is what the governed read tests need."""
    texts = effects.slack_message_texts(live_config, limit=10)

    record(
        "slack",
        Control.UPSTREAM,
        Verdict.PASS,
        detail="channel history readable with the configured token",
        channel=live_config.slack_channel_name or live_config.slack_channel_id,
        messages_read=len(texts),
    )
    assert isinstance(texts, list)


def test_a_posted_message_is_visible_then_removable(
    live_config: LiveConfig, posted: list[str]
) -> None:
    """Certifies the token's write scope, and nothing about governance.

    InterLock cannot write to Slack at all -- `SlackAdapter.execute_write`
    refuses by design. This proves only that the credential could, which is
    worth knowing about the credential and must never be reported as a
    governed write.
    """
    marker = f"{live_config.slack_marker} upstream write certification"
    ts = effects.slack_post(live_config, marker)
    posted.append(ts)

    # Confirmed by re-reading history, not by the post call returning.
    visible = any(marker in text for text in effects.slack_message_texts(live_config, limit=20))

    effects.slack_delete(live_config, ts)
    posted.remove(ts)
    still_visible = any(
        marker in text for text in effects.slack_message_texts(live_config, limit=20)
    )

    record(
        "slack",
        Control.UPSTREAM,
        Verdict.PASS if visible and not still_visible else Verdict.FAIL,
        detail=(
            "slack-sdk post appeared in channel history and was removed; "
            "this certifies the token's scopes, NOT a governed write -- "
            "InterLock refuses Slack writes by design"
        ),
        visible_after_post=visible,
        visible_after_delete=still_visible,
    )

    assert visible, "the posted message never appeared in channel history"
    assert not still_visible, "the deleted message was still visible in channel history"


def test_interlock_refuses_to_write_to_slack(live_config: LiveConfig) -> None:
    """The design decision, pinned so a future change cannot pass unnoticed.

    This is the honest counterpart to the test above. The credential can post;
    the product will not. If someone implements Slack writes, this fails and
    forces the certification matrix to be updated rather than silently
    continuing to claim write safety is not applicable.
    """
    from interlock.connections.connectors import get_adapter

    adapter = get_adapter("slack", {"connector_key": "slack"})

    with pytest.raises(NotImplementedError, match="intentionally disabled"):
        import asyncio

        asyncio.run(
            adapter.execute_write(
                {
                    "source_id": "live_cert_slack",
                    "identity_id": None,
                    "connection_config": {"workspace": "certification"},
                    "query": "post a message",
                }
            )
        )

    record(
        "slack",
        Control.WRITE_SAFETY,
        Verdict.NOT_APPLICABLE,
        detail=(
            "Slack writes are disabled by design in the adapter; refusal proven "
            "rather than assumed. Write safety cannot be certified for a "
            "connector that cannot write."
        ),
        refusal="NotImplementedError",
    )
