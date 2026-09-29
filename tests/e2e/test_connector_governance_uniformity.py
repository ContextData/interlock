"""Governance must apply to every connector, not just the well-trodden ones.

Phase 7 of the governance audit.

Most governance tests exercise one or two connectors - usually HTTP and
PostgreSQL, because those are the ones with the richest fixtures. A connector
that skipped the pipeline, or wired it differently, would pass every one of
them. This file asks the same question of every registered source: is a
request that must be refused actually refused, and is that refusal recorded?

It deliberately parametrises over what the registry contains at runtime rather
than a hand-written list, so a newly registered source is covered the moment
it exists instead of the day someone remembers to add it here.

On what this cannot cover. Seven connectors that the platform ships and
feature-status.md lists as Beta have no source registered in the test stack at
all - not a real upstream, not a mock: elasticsearch, github, gitlab,
google_workspace, slack, snowflake and zendesk. Each has unit and in some
cases integration coverage of its adapter, which is what feature-status.md
claims ("mocked or local adapter tests"), so the documented claim is honest.
What is unverified for those seven is governance: that source roles, policy,
redaction, write safety and audit actually apply when a request reaches them.
That is recorded here rather than left implicit, and is the input to Phase 8.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from interlock.feature_status import GOVERNANCE_UNVERIFIED_CONNECTORS
from tests.e2e.support.clients import mcp_call

pytestmark = [pytest.mark.e2e]

# Registered in the seeded stack, with an upstream that really answers.
_REAL_UPSTREAM = {
    "e2e_pg": "postgresql",
    "e2e_mysql": "mysql (MariaDB container)",
    "e2e_http": "http origin",
    "e2e_s3": "S3 (compose upstream)",
    "e2e_spaces": "S3 (Spaces-compatible upstream)",
    "e2e_opensearch": "OpenSearch",
    "e2e_qdrant": "Qdrant",
}

# Registered, but answered by the mock SaaS server rather than the real product.
_MOCK_UPSTREAM = {
    "e2e_notion": "notion",
    "e2e_salesforce": "salesforce",
}

# Shipped and claimed Beta, with no source registered in this stack.
#
# Two different facts are tracked here and they are not the same fact:
#
#   - absence from the e2e stack, which is what this test can observe; and
#   - whether governance is proven anywhere at all.
#
# Slack and Google Workspace are absent here but certified against real
# tenants in tests/live, so their governance is proven - just not in CI. The
# five in GOVERNANCE_UNVERIFIED_CONNECTORS are proven nowhere, which is why
# that list, not this one, is what docs-site/src/content/docs/reference/feature-status.md publishes. It is
# imported rather than copied so the published claim and the pin cannot drift
# apart; a second hand-maintained list would drift silently, and in the
# direction that flatters the product.
_CERTIFIED_LIVE_ONLY = ("google_workspace", "slack")
_NOT_IN_TEST_STACK = tuple(sorted(GOVERNANCE_UNVERIFIED_CONNECTORS + _CERTIFIED_LIVE_ONLY))


async def _registered_sources(conn: Any) -> list[str]:
    rows = await conn.fetch("SELECT source_id FROM data_sources WHERE enabled ORDER BY source_id")
    return [row["source_id"] for row in rows]


@pytest.mark.asyncio
async def test_no_live_certification_source_survives_into_the_e2e_stack(
    control_db: Any,
) -> None:
    """A leaked `live_cert_*` source would point this suite at production.

    The tests below enumerate every enabled source and fire a query at each.
    The live certification harness registers real managed databases, a real S3
    bucket, a real Slack workspace and a real Google tenant under the
    `live_cert_` prefix, and removes them in a `finally` - but `finally` does
    not survive a SIGKILL.

    If one survives, the suite still passes today, because the blocked
    identity it probes with holds no grant on those sources and is refused
    before the connector is reached. That is the right outcome for the wrong
    reason: it depends on the grant table rather than on isolation. This
    assertion makes the leak fail loudly instead of resting on that.

    Recovery: `make live-teardown`, or `make live-sweep` for a killed run.
    """
    leaked = await control_db.fetch(
        "SELECT source_id FROM data_sources WHERE enabled AND source_id LIKE 'live_cert_%'"
    )

    assert not leaked, (
        "live certification sources are still registered: "
        f"{[row['source_id'] for row in leaked]}. This suite queries every enabled "
        "source, so running it now would send traffic to real production systems. "
        "Run `make live-teardown` (or `make live-sweep`) before `make test-e2e`."
    )


@pytest.mark.asyncio
async def test_every_registered_source_refuses_a_blocked_identity(
    e2e_config: Any, control_db: Any
) -> None:
    """A blocked identity must be refused on every source, not just the tested ones.

    The seeded denied agent holds a `blocked` role on the seeded sources. If a
    connector reached its upstream without consulting the pipeline, this is
    where it would show: the request would succeed on that source alone while
    every other source refused it.
    """
    sources = await _registered_sources(control_db)
    assert sources, "no enabled sources are registered"

    allowed_through: list[str] = []
    for source_id in sources:
        response = mcp_call(
            e2e_config,
            "interlock_query",
            {"source_id": source_id, "sql": "SELECT 1"},
            headers={"Authorization": f"Bearer {e2e_config.denied_api_key}"},
        )
        body = response.text.lower()
        # Requires a *governance* refusal, not merely an error. Accepting any
        # error would let a connector that fails for an unrelated reason - a
        # malformed query, an unreachable upstream - masquerade as a working
        # control, which is precisely the kind of test that cannot fail.
        refused = response.status_code == 403 and "denied" in body
        if not refused:
            allowed_through.append(f"{source_id} ({response.status_code}: {body[:60]})")

    assert not allowed_through, (
        "a blocked identity was not refused on: "
        f"{allowed_through} - these connectors are not consulting the governance pipeline"
    )


@pytest.mark.asyncio
async def test_every_registered_source_audits_a_refused_request(
    e2e_config: Any, control_db: Any
) -> None:
    """Refusal is not enough; the refusal has to be recorded.

    A connector that refuses without auditing leaves no evidence that an agent
    attempted the access, which is the record an incident review depends on.
    """
    sources = await _registered_sources(control_db)
    before = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))

    for source_id in sources:
        mcp_call(
            e2e_config,
            "interlock_query",
            {"source_id": source_id, "sql": "SELECT 1"},
            headers={"Authorization": f"Bearer {e2e_config.denied_api_key}"},
        )

    audited: set[str] = set()
    for _ in range(20):
        rows = await control_db.fetch(
            "SELECT DISTINCT source_id FROM audit_log WHERE id > $1 AND status = 'denied'",
            before,
        )
        audited = {row["source_id"] for row in rows if row["source_id"]}
        if audited.issuperset(set(sources)):
            break
        await asyncio.sleep(0.25)

    missing = sorted(set(sources) - audited)
    assert not missing, f"a refused request left no audit row for: {missing}"


def test_the_unverified_connectors_are_recorded_not_forgotten() -> None:
    """Pins the list of connectors whose governance is unverified end to end.

    This is a bookkeeping test, and deliberately so. Its job is to fail when
    the set changes, so that adding a connector forces a decision about
    whether it gets governance coverage or gets recorded as unproven - rather
    than the list quietly drifting out of date in a document.
    """
    from interlock.connections.connectors import CONNECTOR_DEFINITIONS

    available = {key for key, spec in CONNECTOR_DEFINITIONS.items() if spec.status != "planned"}
    covered = {
        "postgresql",
        "generic_rest",
        "mysql",
        "mariadb",
        "s3",
        "digitalocean_spaces",
        "opensearch",
        "qdrant",
        "notion",
        "salesforce",
    }

    unverified = sorted(available - covered)
    assert unverified == sorted(_NOT_IN_TEST_STACK), (
        "the set of connectors without governance coverage has changed.\n"
        f"  now unverified: {unverified}\n"
        f"  recorded:       {sorted(_NOT_IN_TEST_STACK)}\n"
        "Add a registered source for the new connector, or add it to the "
        "recorded list and reflect it in docs-site/src/content/docs/reference/feature-status.md."
    )


def test_the_classification_matches_the_seeded_stack() -> None:
    """The three buckets above must together account for every connector."""
    from interlock.connections.connectors import CONNECTOR_DEFINITIONS

    available = {key for key, spec in CONNECTOR_DEFINITIONS.items() if spec.status != "planned"}
    assert len(_REAL_UPSTREAM) == 7
    assert len(_MOCK_UPSTREAM) == 2
    assert len(_NOT_IN_TEST_STACK) == 7
    # 7 real sources cover 8 connector keys: mysql and mariadb share an adapter
    # and a container, so e2e_mysql exercises both.
    assert len(available) == 17, f"connector count changed: {len(available)}"
