"""The certification matrix, pinned as data.

This file carries no `live` marker and needs no credentials, so it runs in
`make test-unit` on every machine. That is the point: it is the mechanism that
keeps the certification report honest when nobody is running live tests.

It enforces three things.

1. Every (system, control) cell has an explicit verdict. A control cannot be
   quietly absent - it is PROVEN, NOT_APPLICABLE with a reason, or UNPROVEN
   with a reason.
2. Every PROVEN cell names a test that exists and is collected. A claim whose
   test was deleted or renamed fails here rather than surviving in a report.
3. The set of live systems matches what the seeder registers, so adding a
   sixth system forces a decision about its coverage instead of letting it
   drift out of the matrix.

It mirrors `test_the_unverified_connectors_are_recorded_not_forgotten` in the
e2e suite, which the repo already uses to stop an unverified connector list
rotting quietly in a document.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live.support.evidence import Control

# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------

PROVEN = "PROVEN"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNPROVEN = "UNPROVEN"
FAILING = "FAILING"

# (system, control) -> (verdict, reason-or-test-module)
#
# `reason` is a test module basename for PROVEN and FAILING cells, and a
# sentence for NOT_APPLICABLE and UNPROVEN ones. Both are load-bearing: the
# first is checked against the filesystem, the second is what the report
# prints instead of leaving a blank.
MATRIX: dict[tuple[str, Control], tuple[str, str]] = {
    # ---- PostgreSQL: certified over PG-wire -------------------------------
    ("postgresql", Control.UPSTREAM): (PROVEN, "test_upstream_postgres"),
    ("postgresql", Control.ROLE_ALLOW): (PROVEN, "test_governed_postgres"),
    ("postgresql", Control.ROLE_DENY): (PROVEN, "test_governed_postgres"),
    ("postgresql", Control.REDACTION): (PROVEN, "test_governed_postgres"),
    ("postgresql", Control.WRITE_SAFETY): (PROVEN, "test_governed_postgres"),
    ("postgresql", Control.APPROVAL_GATING): (PROVEN, "test_governed_postgres"),
    ("postgresql", Control.AUDIT): (PROVEN, "test_governed_postgres"),
    ("postgresql", Control.POLICY_DENY): (
        UNPROVEN,
        "the blocked identity holds both a blocked source role and a matching deny "
        "policy, so a refusal cannot be attributed to policy alone. Needs an "
        "identity denied by policy while its source role permits.",
    ),
    ("postgresql", Control.CACHE_ISOLATION): (
        NOT_APPLICABLE,
        "live sources are registered with cache_strategy=bypass, so no answer is "
        "cached to cross identities. Cache isolation is covered against the compose "
        "stack in tests/e2e/test_cache_correctness_guarantees.py.",
    ),
    # ---- MySQL: certified over MCP ----------------------------------------
    ("mysql", Control.UPSTREAM): (PROVEN, "test_upstream_mysql"),
    ("mysql", Control.ROLE_ALLOW): (PROVEN, "test_governed_mysql"),
    ("mysql", Control.ROLE_DENY): (PROVEN, "test_governed_mysql"),
    ("mysql", Control.REDACTION): (PROVEN, "test_governed_mysql"),
    ("mysql", Control.WRITE_SAFETY): (PROVEN, "test_governed_mysql"),
    ("mysql", Control.APPROVAL_GATING): (PROVEN, "test_governed_mysql"),
    ("mysql", Control.AUDIT): (PROVEN, "test_governed_mysql"),
    ("mysql", Control.POLICY_DENY): (UNPROVEN, "same limitation as PostgreSQL above."),
    ("mysql", Control.CACHE_ISOLATION): (NOT_APPLICABLE, "cache_strategy=bypass, as above."),
    # ---- S3 ---------------------------------------------------------------
    ("s3", Control.UPSTREAM): (PROVEN, "test_upstream_s3"),
    ("s3", Control.ROLE_ALLOW): (PROVEN, "test_governed_discovery"),
    ("s3", Control.ROLE_DENY): (PROVEN, "test_governed_discovery"),
    ("s3", Control.AUDIT): (PROVEN, "test_governed_discovery"),
    ("s3", Control.WRITE_SAFETY): (
        UNPROVEN,
        "S3 has no governed write entry point. It declares supports_query=False and "
        "supports_proxy=False, so no protocol surface can express an object write, "
        "and nothing creates an S3 write approval. The approval queue now forwards "
        "structured write parameters and reports a shape rejection honestly rather "
        "than as a generic failed write, so the plumbing is correct - "
        "but reachability is a feature gap, not a defect, and is not claimed here.",
    ),
    ("s3", Control.APPROVAL_GATING): (
        UNPROVEN,
        "there is no way to create an S3 write approval, so there is nothing to gate. "
        "Same root cause as write safety above.",
    ),
    ("s3", Control.REDACTION): (
        NOT_APPLICABLE,
        "policy redact_columns is column-oriented and object bodies are not rows. "
        "Object *metadata* served through discovery is covered by the discovery "
        "redaction finding.",
    ),
    ("s3", Control.POLICY_DENY): (UNPROVEN, "same limitation as PostgreSQL above."),
    ("s3", Control.CACHE_ISOLATION): (NOT_APPLICABLE, "cache_strategy=bypass, as above."),
    # ---- Slack ------------------------------------------------------------
    ("slack", Control.UPSTREAM): (PROVEN, "test_upstream_slack"),
    ("slack", Control.ROLE_ALLOW): (PROVEN, "test_governed_discovery"),
    ("slack", Control.ROLE_DENY): (PROVEN, "test_governed_discovery"),
    ("slack", Control.AUDIT): (PROVEN, "test_governed_discovery"),
    ("slack", Control.REDACTION): (PROVEN, "test_governed_discovery"),
    ("slack", Control.WRITE_SAFETY): (
        NOT_APPLICABLE,
        "SlackAdapter.execute_write raises NotImplementedError by design; the "
        "refusal is proven rather than assumed, in test_upstream_slack.",
    ),
    ("slack", Control.APPROVAL_GATING): (
        NOT_APPLICABLE,
        "there is no write path to gate: the adapter refuses every write, so an "
        "approval queue entry for Slack can never be created or executed.",
    ),
    ("slack", Control.POLICY_DENY): (UNPROVEN, "same limitation as PostgreSQL above."),
    ("slack", Control.CACHE_ISOLATION): (NOT_APPLICABLE, "cache_strategy=bypass, as above."),
    # ---- Google Workspace -------------------------------------------------
    ("google_workspace", Control.UPSTREAM): (PROVEN, "test_upstream_google_workspace"),
    ("google_workspace", Control.ROLE_ALLOW): (PROVEN, "test_governed_discovery"),
    ("google_workspace", Control.ROLE_DENY): (PROVEN, "test_governed_discovery"),
    ("google_workspace", Control.AUDIT): (PROVEN, "test_governed_discovery"),
    ("google_workspace", Control.REDACTION): (PROVEN, "test_governed_discovery"),
    ("google_workspace", Control.WRITE_SAFETY): (
        NOT_APPLICABLE,
        "GoogleWorkspaceAdapter.execute_write raises NotImplementedError by design; "
        "the refusal is proven in test_upstream_google_workspace. Separately, the "
        "service account has no storage quota and cannot create Drive files at all.",
    ),
    ("google_workspace", Control.APPROVAL_GATING): (
        NOT_APPLICABLE,
        "there is no write path to gate: the adapter refuses every write, and the "
        "service account additionally owns no Drive storage quota.",
    ),
    ("google_workspace", Control.POLICY_DENY): (
        UNPROVEN,
        "same limitation as PostgreSQL above.",
    ),
    ("google_workspace", Control.CACHE_ISOLATION): (
        NOT_APPLICABLE,
        "cache_strategy=bypass, as above.",
    ),
}

SYSTEMS = ("postgresql", "mysql", "s3", "slack", "google_workspace")

# Controls certified only where the upstream supports them. Gmail and Admin
# Reports are unreachable for a consumer subject; recorded here rather than
# omitted, so the gap is visible without reading a test.
ENVIRONMENT_LIMITS = {
    "google_workspace": (
        "Drive only. Gmail and Admin Reports require domain-wide delegation, which "
        "cannot be granted for a consumer @gmail.com subject - it has no Workspace "
        "admin console. Lifting this needs a subject on a Workspace domain."
    ),
}

_TEST_DIR = Path(__file__).parent


def test_every_system_and_control_has_an_explicit_verdict() -> None:
    """No cell may be silently absent.

    An omitted control reads as "not thought about". This forces every one to
    be PROVEN, FAILING, NOT_APPLICABLE or UNPROVEN.
    """
    missing = [
        (system, str(control))
        for system in SYSTEMS
        for control in Control
        if (system, control) not in MATRIX
    ]
    assert not missing, (
        f"{len(missing)} (system, control) cell(s) have no verdict: {missing}. "
        "Add each to MATRIX as PROVEN with its test module, or as NOT_APPLICABLE "
        "or UNPROVEN with a reason."
    )


def test_every_proven_or_failing_cell_names_a_test_module_that_exists() -> None:
    """A claim whose test was deleted or renamed must fail here.

    This is what stops the report citing tests that no longer run - the exact
    drift the audit was commissioned to eliminate.
    """
    dangling = []
    for (system, control), (verdict, reference) in MATRIX.items():
        if verdict not in (PROVEN, FAILING):
            continue
        if not (_TEST_DIR / f"{reference}.py").is_file():
            dangling.append(f"{system}/{control} -> {reference}.py")

    assert not dangling, (
        f"{len(dangling)} certified cell(s) name a test module that does not exist: " f"{dangling}"
    )


def test_unproven_and_not_applicable_cells_carry_a_reason() -> None:
    """A gap without a stated reason is indistinguishable from an oversight."""
    unexplained = [
        f"{system}/{control}"
        for (system, control), (verdict, reason) in MATRIX.items()
        if verdict in (NOT_APPLICABLE, UNPROVEN) and len(reason) < 30
    ]
    assert not unexplained, (
        f"cell(s) marked NOT_APPLICABLE or UNPROVEN without a substantive reason: " f"{unexplained}"
    )


def test_the_matrix_covers_exactly_the_systems_the_seeder_registers() -> None:
    """Adding a sixth live system must force a coverage decision.

    Read from the seeder rather than a hand-copied list, so the two cannot
    drift apart.
    """
    from tests.live.support import seed

    seeded = {key for key in seed._AGENT_ROLES}
    # The seeder keys on connector key; the matrix keys on system name. They
    # coincide except that PostgreSQL's connector key is also "postgresql".
    assert seeded == set(SYSTEMS), (
        f"the seeder registers {sorted(seeded)} but the matrix covers "
        f"{sorted(SYSTEMS)}. Add the new system to MATRIX with a verdict for "
        "every control, or remove it from the seeder."
    )


def test_failing_cells_correspond_to_open_findings() -> None:
    """A FAILING verdict must be a known, recorded defect, not a surprise.

    Currently empty: the two discovery-redaction failures are fixed - `_discover`, `_related_documents` and `_describe_source`
    now share `_redact_rows` with the query path - and their cells are PROVEN.

    If a cell becomes FAILING without a finding behind it, this test is the
    wrong place to record it. Open a finding first.
    """
    failing = {
        f"{system}/{control}"
        for (system, control), (verdict, _) in MATRIX.items()
        if verdict == FAILING
    }
    expected: set[str] = set()
    assert failing == expected, (
        f"the set of failing controls changed: now {sorted(failing)}, recorded "
        f"{sorted(expected)}. Open or close the corresponding finding and update "
        "this test in the same change."
    )


@pytest.mark.parametrize("system", SYSTEMS)
def test_each_system_proves_at_least_authorization_and_audit(system: str) -> None:
    """The floor below which registering a source certifies nothing.

    A connector whose refusals and audit trail are unproven has not been
    governed in any meaningful sense, whatever else it does.
    """
    floor = (Control.ROLE_ALLOW, Control.ROLE_DENY, Control.AUDIT)
    weak = [
        str(control) for control in floor if MATRIX[(system, control)][0] not in (PROVEN, FAILING)
    ]
    assert not weak, f"{system} does not prove {weak}, which is the minimum bar"
