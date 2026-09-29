"""Cache correctness: what the key must separate, and what it actually does.

Phase 5 of the governance audit.

A governance cache has one job beyond speed: never serve an answer computed
under one caller's authority to a caller with different authority. The cache
key is where that guarantee lives, so these tests assert on the key's
behaviour directly rather than trying to provoke a leak through the proxy -
a collision is the defect, and waiting for it to become visible in a response
means waiting for the disclosure.

These all pass. One of them did not always: it was written as a strict xfail
on the belief that `source_role_scope_hash` - a declared key dimension no call
site populated - was the only thing carrying grant scope into the key. That
belief was wrong, and the mistake is recorded in the test rather than quietly
removed, because it is the more useful half. Source-role scope reaches the key
by a different door: `_decision_scope_hash` folds the source-role decision into
its payload and the call sites pass that digest as `policy_scope_hash`.

The dead parameter has since been removed and the key version bumped to v5, so
nothing here refers to a dimension that no longer exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from interlock.core.normalizer import compute_cache_key

pytestmark = [pytest.mark.e2e]


def _key(**overrides: Any) -> str:
    """A realistic key, with one dimension varied per test."""
    base: dict[str, Any] = {
        "source_id": "audit_src",
        "normalized_sql": "SELECT * FROM customers",
        "identity_role": "reader",
        "mapped_pg_role": "ro",
        "tenant_id": "acme",
        "grants_version": "1",
        "policy_scope_hash": "policy-v1",
        "source_generation": 3,
    }
    base.update(overrides)
    return compute_cache_key(**base)


def test_the_same_request_under_the_same_authority_is_cacheable() -> None:
    """The baseline. Without a stable key there is no cache to reason about."""
    assert _key() == _key()


def test_a_different_source_never_shares_an_entry() -> None:
    assert _key(source_id="a") != _key(source_id="b")


def test_a_different_identity_role_never_shares_an_entry() -> None:
    assert _key(identity_role="reader") != _key(identity_role="analyst")


def test_a_different_mapped_pg_role_never_shares_an_entry() -> None:
    """The upstream role decides what the query can see, so it must key."""
    assert _key(mapped_pg_role="ro") != _key(mapped_pg_role="rw")


def test_a_different_tenant_never_shares_an_entry() -> None:
    assert _key(tenant_id="acme") != _key(tenant_id="globex")


def test_changing_the_policy_invalidates_the_entry() -> None:
    """A policy change must not keep serving the answer computed under the old one."""
    assert _key(policy_scope_hash="policy-v1") != _key(policy_scope_hash="policy-v2")


def test_a_write_invalidates_the_entry_through_the_source_generation() -> None:
    """The write barrier: after a write the generation advances and old keys miss."""
    assert _key(source_generation=3) != _key(source_generation=4)


def test_revoking_and_regranting_invalidates_through_grants_version() -> None:
    assert _key(grants_version="1") != _key(grants_version="2")


def test_different_source_role_grants_never_share_an_entry() -> None:
    """Two agents with different grants must not share a cached answer.

    Written first as a strict xfail, on the belief that a declared parameter
    no call site populated was the only thing carrying grant scope into the
    key. That was wrong, and the error is the part worth keeping:
    `_decision_scope_hash` already folds the source-role decision into its
    payload, and the call sites pass that digest as `policy_scope_hash`, so
    grant scope reaches the key by a different door.

    The demonstration that produced the finding held `policy_scope_hash`
    constant across both calls - which cannot happen in practice, precisely
    because that value derives from the source-role decision. Holding an input
    constant that varies with the thing under test manufactures the collision
    it claims to find.

    The dead parameter has since been removed and the key format
    version bumped accordingly.
    """
    from interlock.core.source_roles import SourceRoleDecision
    from interlock.gateway.pg_proxy import _decision_scope_hash
    from interlock.gateway.pipeline import GatewayDecision

    narrow = _decision_scope_hash(
        GatewayDecision(
            allowed=True,
            source_role_decision=SourceRoleDecision(
                allowed=True, reason="ok", matched_role_ids=[1], matched_permission_ids=[10]
            ),
        )
    )
    broad = _decision_scope_hash(
        GatewayDecision(
            allowed=True,
            source_role_decision=SourceRoleDecision(
                allowed=True, reason="ok", matched_role_ids=[2], matched_permission_ids=[20, 21]
            ),
        )
    )

    assert narrow != broad, "two different source-role decisions produced the same scope digest"
    assert _key(policy_scope_hash=narrow) != _key(
        policy_scope_hash=broad
    ), "two identities with different source-role grants produced the same cache key"
