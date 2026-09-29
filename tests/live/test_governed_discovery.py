"""Tier 1: governance over the discovery connectors - S3, Slack, Google Drive.

These three declare `supports_query=False`, so `interlock_query` against them
is refused by design and their governed path is `interlock_discover`.
Discovery searches a catalogue rather than the upstream directly, which raises
an obvious objection: certifying governance over hand-written fixtures would
prove nothing about real data.

So `tests/live/support/discovery.py` indexes what the upstreams *actually*
contain - real object keys, real channel messages, real Drive files, read
through `effects.py`. A discovery result here is a statement about the live
system, and redaction observed on one is redaction of real content.

The redaction tests were written to prove the strongest control available for
connectors that cannot write, and initially proved the opposite: a Slack
message and a Drive document, each carrying a synthetic SSN, were both
returned verbatim by `interlock_discover` to a *granted* agent while
`interlock_query` redacted the identical literal in the same run. Finding it on two unrelated connectors is what established it
as a property of the discovery handler rather than of one adapter.

It is fixed: `_discover`, `_related_documents` and `_describe_source` now
share `_redact_rows` with the query path. Both tests assert redaction *and*
that the asset is still returned - checking only for the SSN's absence would
pass just as well if discovery returned nothing at all.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.live.support import discovery, effects
from tests.live.support.config import LiveConfig, load_live_config
from tests.live.support.evidence import Control, Verdict, record
from tests.live.support.seed import AGENT_API_KEY, BLOCKED_API_KEY, source_id

pytestmark = [pytest.mark.live]

SSN = "123-45-6789"


def _discover(cfg: LiveConfig, api_key: str, source: str, query: str) -> httpx.Response:
    return httpx.post(
        f"{cfg.e2e.gateway_url}/mcp/tools/call",
        json={
            "name": "interlock_discover",
            "arguments": {"source_id": source, "query": query},
        },
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60,
    )


@pytest.fixture(scope="module", autouse=True)
def indexed(live_config: LiveConfig) -> Iterator[dict[str, int]]:
    counts = asyncio.run(discovery.index_all(live_config))
    try:
        yield counts
    finally:
        asyncio.run(discovery.teardown_discovery(live_config))


# --------------------------------------------------------------------------
# S3
# --------------------------------------------------------------------------

s3_only = pytest.mark.skipif(
    not load_live_config().has_s3(), reason="live S3 credentials are not configured"
)


@s3_only
def test_s3_discovery_returns_real_objects(live_config: LiveConfig) -> None:
    """What the gateway returns must correspond to what the bucket holds."""
    response = _discover(live_config, AGENT_API_KEY, source_id("s3"), "live-cert")
    upstream = effects.s3_object_keys(live_config, prefix=live_config.s3_prefix)

    served_real_key = any(key.rsplit("/", 1)[-1] in response.text for key in upstream[:10])

    record(
        source_id("s3"),
        Control.ROLE_ALLOW,
        Verdict.PASS if response.status_code == 200 and served_real_key else Verdict.FAIL,
        detail="discovery returned objects that genuinely exist in the live bucket",
        status=response.status_code,
        objects_in_bucket=len(upstream),
    )
    assert response.status_code == 200, response.text[:200]
    assert served_real_key, "discovery returned nothing matching a real object key"


@s3_only
def test_s3_refuses_a_blocked_identity(live_config: LiveConfig) -> None:
    response = _discover(live_config, BLOCKED_API_KEY, source_id("s3"), "live-cert")
    body = response.text.lower()

    record(
        source_id("s3"),
        Control.ROLE_DENY,
        Verdict.PASS if response.status_code == 403 and "denied" in body else Verdict.FAIL,
        detail="a blocked identity was refused discovery by governance",
        status=response.status_code,
    )
    assert response.status_code == 403, f"expected a refusal, got {response.status_code}"
    assert "denied" in body


@s3_only
def test_s3_query_is_refused_because_the_connector_declares_no_query_support(
    live_config: LiveConfig,
) -> None:
    """Correct behaviour, pinned so it is not mistaken for a defect.

    S3 declares supports_query=False. `interlock_query` therefore produces a
    database-shaped permission request (`db.execute_raw`, resource_type
    `db.table`) that no storage-shaped role can satisfy, and the request is
    refused. That is the connector's capability contract working, not a gap -
    worth pinning because the refusal looks identical to a misconfiguration.
    """
    response = httpx.post(
        f"{live_config.e2e.gateway_url}/mcp/tools/call",
        json={
            "name": "interlock_query",
            "arguments": {"source_id": source_id("s3"), "sql": "SELECT 1"},
        },
        headers={"Authorization": f"Bearer {AGENT_API_KEY}"},
        timeout=60,
    )

    record(
        source_id("s3"),
        Control.ROLE_DENY,
        Verdict.NOT_APPLICABLE,
        detail=(
            "interlock_query is refused for S3 because the connector declares "
            "supports_query=False; its governed path is discovery"
        ),
        status=response.status_code,
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------

slack_only = pytest.mark.skipif(
    not load_live_config().has_slack(), reason="live Slack credentials are not configured"
)


@slack_only
def test_slack_discovery_returns_real_messages(live_config: LiveConfig) -> None:
    response = _discover(live_config, AGENT_API_KEY, source_id("slack"), "live-cert")

    record(
        source_id("slack"),
        Control.ROLE_ALLOW,
        Verdict.PASS if response.status_code == 200 else Verdict.FAIL,
        detail="discovery returned messages indexed from the live channel",
        status=response.status_code,
    )
    assert response.status_code == 200, response.text[:200]
    assert "slack://channel/" in response.text, response.text[:200]


@slack_only
def test_slack_refuses_a_blocked_identity(live_config: LiveConfig) -> None:
    response = _discover(live_config, BLOCKED_API_KEY, source_id("slack"), "live-cert")

    record(
        source_id("slack"),
        Control.ROLE_DENY,
        Verdict.PASS if response.status_code == 403 else Verdict.FAIL,
        detail="a blocked identity was refused discovery by governance",
        status=response.status_code,
    )
    assert response.status_code == 403
    assert "denied" in response.text.lower()


@slack_only
def test_pii_in_a_real_slack_message_is_redacted_by_discovery(
    live_config: LiveConfig,
) -> None:
    """Redaction over a connector that cannot write - and the fix for #48.

    This test originally found the opposite of what it was written to prove:
    `interlock_discover` returned a real message's synthetic SSN verbatim to a
    granted agent while `interlock_query` redacted the identical literal in
    the same run. `_execute_query` had a PII-redaction step; `_discover`,
    `_related_documents` and `_describe_source` had none, contradicting
    `docs-site/src/content/docs/reference/contracts/mcp-v1.md`, which scopes redaction to tool execution
    generally.

    All four now share `_redact_rows`. Both halves are asserted, because
    checking only that the SSN is absent would pass just as well if discovery
    returned nothing at all: the message must still be found, and the upstream
    must still hold the raw value.
    """
    marker = f"{live_config.slack_marker} redaction probe ssn {SSN}"
    ts = effects.slack_post(live_config, marker)
    try:
        asyncio.run(discovery.index_all(live_config))
        discovered = _discover(live_config, AGENT_API_KEY, source_id("slack"), "redaction probe")
        upstream_texts = effects.slack_message_texts(live_config, limit=20)

        # The control case: the same literal through the query path.
        queried = httpx.post(
            f"{live_config.e2e.gateway_url}/mcp/tools/call",
            json={
                "name": "interlock_query",
                "arguments": {
                    "source_id": source_id("mysql"),
                    "sql": f"SELECT '{SSN}' AS ssn",
                },
            },
            headers={"Authorization": f"Bearer {AGENT_API_KEY}"},
            timeout=60,
        )

        upstream_has_ssn = any(SSN in text for text in upstream_texts)
        discovery_leaked = SSN in discovered.text
        query_leaked = SSN in queried.text

        found_the_message = "postfix" in discovered.text or "redaction probe" in discovered.text

        record(
            source_id("slack"),
            Control.REDACTION,
            (
                Verdict.PASS
                if found_the_message and not discovery_leaked and upstream_has_ssn
                else Verdict.FAIL
            ),
            detail=(
                "a real Slack message's synthetic SSN was redacted by "
                "interlock_discover while Slack itself still served it in full, and "
                "the message was still returned rather than suppressed"
            ),
            discovery_leaked_ssn=discovery_leaked,
            query_leaked_ssn=query_leaked,
            upstream_still_holds_ssn=upstream_has_ssn,
        )

        assert upstream_has_ssn, "precondition: Slack does not hold the probe message"
        assert not query_leaked, "the query path leaked the SSN"
        assert found_the_message, (
            f"discovery returned nothing for the probe message, so the absence of the "
            f"SSN proves nothing: {discovered.text[:200]}"
        )
        assert not discovery_leaked, f"discovery leaked the SSN unredacted: {discovered.text[:300]}"
    finally:
        effects.slack_delete(live_config, ts)


# --------------------------------------------------------------------------
# Google Drive
# --------------------------------------------------------------------------

gws_only = pytest.mark.skipif(
    not load_live_config().has_google_workspace(),
    reason="live Google Workspace service account is not configured",
)


@gws_only
def test_drive_discovery_returns_shared_files(
    live_config: LiveConfig, indexed: dict[str, int]
) -> None:
    if not indexed.get("google_workspace"):
        pytest.skip(
            f"no files are shared with {live_config.gws_client_email} in "
            f"{live_config.gws_drive_folder_name!r}; place one to certify Drive discovery"
        )

    response = _discover(live_config, AGENT_API_KEY, source_id("google_workspace"), "live-cert")

    record(
        source_id("google_workspace"),
        Control.ROLE_ALLOW,
        Verdict.PASS if response.status_code == 200 else Verdict.FAIL,
        detail="discovery returned documents indexed from the shared Drive folder",
        status=response.status_code,
        files_indexed=indexed.get("google_workspace", 0),
    )
    assert response.status_code == 200, response.text[:200]


@gws_only
def test_pii_in_a_real_drive_document_is_redacted_by_discovery(
    live_config: LiveConfig, indexed: dict[str, int]
) -> None:
    """The Drive half of the #48 fix, on a second connector.

    A human-placed document in the shared folder carries a synthetic SSN. Its
    *content* is indexed - not just its filename, which would leave no PII in
    the catalogue and make this unfalsifiable.

    Proving it on a second, unrelated connector is what establishes that the
    fix lives in the discovery handler rather than in one adapter.
    """
    if not indexed.get("google_workspace"):
        pytest.skip(
            f"no files are shared with {live_config.gws_client_email} in "
            f"{live_config.gws_drive_folder_name!r}"
        )

    folder = effects.drive_folder_id(live_config)
    upstream_text = " ".join(
        effects.drive_text(live_config, f["id"], f.get("mimeType", ""))
        for f in effects.drive_files(live_config, folder_id=folder)
    )
    if not re.search(r"\d{3}-\d{2}-\d{4}", upstream_text):
        pytest.skip(
            "no shared Drive document contains an SSN-shaped value; add one to "
            "certify Drive redaction"
        )
    ssn = re.search(r"\d{3}-\d{2}-\d{4}", upstream_text).group(0)

    response = _discover(live_config, AGENT_API_KEY, source_id("google_workspace"), "live-cert")
    leaked = ssn in response.text

    returned_something = "gdrive://" in response.text

    record(
        source_id("google_workspace"),
        Control.REDACTION,
        Verdict.PASS if returned_something and not leaked else Verdict.FAIL,
        detail=(
            "a Drive document's SSN was redacted by interlock_discover while the "
            "document itself still holds it. Confirms the #48 fix on a second "
            "connector, which is what shows it is a property of the discovery "
            "handler rather than of one adapter."
        ),
        discovery_leaked_ssn=leaked,
        upstream_document_has_ssn=True,
    )

    assert returned_something, (
        f"discovery returned no Drive asset, so the absence of the SSN proves "
        f"nothing: {response.text[:200]}"
    )
    assert not leaked, f"Drive discovery leaked the document's SSN: {response.text[:300]}"


@gws_only
def test_drive_refuses_a_blocked_identity(live_config: LiveConfig) -> None:
    """Provable regardless of whether anything is shared.

    Governance is evaluated before the connector is reached, so an empty
    folder does not weaken this one.
    """
    response = _discover(live_config, BLOCKED_API_KEY, source_id("google_workspace"), "live-cert")

    record(
        source_id("google_workspace"),
        Control.ROLE_DENY,
        Verdict.PASS if response.status_code == 403 else Verdict.FAIL,
        detail="a blocked identity was refused discovery by governance",
        status=response.status_code,
    )
    assert response.status_code == 403
    assert "denied" in response.text.lower()


# --------------------------------------------------------------------------
# Audit, across all three
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("system", ["s3", "slack", "google_workspace"])
async def test_a_refused_discovery_is_audited(
    live_config: LiveConfig, control_plane: Any, system: str
) -> None:
    """A refusal that leaves no record is indistinguishable from no request."""
    source = source_id(system)
    before = int(await control_plane.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
    _discover(live_config, BLOCKED_API_KEY, source, "live-cert")

    rows: list[Any] = []
    for _ in range(24):
        rows = await control_plane.fetch(
            "SELECT id, status FROM audit_log WHERE id > $1 AND source_id = $2 AND status = 'denied'",
            before,
            source,
        )
        if rows:
            break
        await asyncio.sleep(0.25)

    record(
        source,
        Control.AUDIT,
        Verdict.PASS if rows else Verdict.FAIL,
        detail="the refused discovery request produced a denied audit row",
        audit_rows=len(rows),
    )
    assert rows, f"a refused discovery against {source} left no audit row"
