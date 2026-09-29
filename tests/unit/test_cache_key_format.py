"""The cache key's shape, and the version tag that guards changes to it.

The key is a positional, separator-delimited payload hashed with SHA-256.
Adding, removing or reordering a field changes every key, so the payload
carries a version tag as its first element - and the tag has to move whenever
the shape does, or entries written by an older build become unreachable
without anyone deciding that.

`source_role_scope_hash` was removed here. It was a declared
dimension no call site ever populated, so it read as security-relevant while
being permanently empty - which is what led an earlier audit to report a
cache-isolation hole that did not exist. Source-role scope does reach the key,
through the digest passed as `policy_scope_hash`.
"""

from __future__ import annotations

import inspect

import pytest

from interlock.core.normalizer import compute_cache_key

BASE = {
    "source_id": "src",
    "normalized_sql": "SELECT * FROM t",
}


def test_the_removed_parameter_is_gone_from_the_signature() -> None:
    """A caller passing it should fail loudly rather than be silently ignored."""
    parameters = inspect.signature(compute_cache_key).parameters

    assert "source_role_scope_hash" not in parameters
    with pytest.raises(TypeError):
        compute_cache_key(**BASE, source_role_scope_hash="x")  # type: ignore[call-arg]


def test_the_payload_carries_the_current_version_tag() -> None:
    """Pins the tag to the shape.

    If a field is added or removed without moving this, entries written by the
    previous build stay in Redis under keys nothing will ever look up again -
    harmless but invisible, and the tag exists precisely so the break is a
    decision rather than an accident.
    """
    source = inspect.getsource(compute_cache_key)

    assert '"v5"' in source, (
        "the cache key version tag is no longer v5. If the payload shape changed, "
        "bump it and update this test in the same commit; if it did not, restore it."
    )
    assert '"v4"' not in source


@pytest.mark.parametrize(
    "field",
    [
        "identity_role",
        "mapped_pg_role",
        "tenant_id",
        "grants_version",
        "policy_scope_hash",
    ],
)
def test_every_declared_dimension_actually_separates_keys(field: str) -> None:
    """No parameter may be inert.

    A dimension that does not change the key is worse than an absent one: it
    reads as isolation that is not there, which is exactly how the removed
    parameter misled an audit.
    """
    one = compute_cache_key(**BASE, **{field: "a"})
    two = compute_cache_key(**BASE, **{field: "b"})

    assert one != two, f"{field} does not affect the cache key"


def test_the_generation_separates_keys() -> None:
    """The write barrier: after a write the generation advances and old keys miss."""
    assert compute_cache_key(**BASE, source_generation=3) != compute_cache_key(
        **BASE, source_generation=4
    )


def test_parameters_separate_keys() -> None:
    assert compute_cache_key(**BASE, parameters=[1]) != compute_cache_key(**BASE, parameters=[2])


def test_the_same_inputs_are_stable_across_calls() -> None:
    """Without this there is no cache to reason about."""
    assert compute_cache_key(**BASE, identity_role="r") == compute_cache_key(
        **BASE, identity_role="r"
    )


def test_absent_and_empty_are_the_same_by_design() -> None:
    """Both render as an empty field, so they must not produce different keys.

    Worth pinning rather than leaving implicit: a caller that passes "" where
    another passes None would otherwise silently miss the other's entries.
    """
    assert compute_cache_key(**BASE, tenant_id=None) == compute_cache_key(**BASE, tenant_id="")
