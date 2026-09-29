"""Regression test for audit P0-C: cache keys must include identity context.

AUDIT-COVERS: P0-C

The audit reported that ``pg_proxy.py:396`` called ``normalize_sql``
without passing identity, role, policy scope, or row-level-security
context. Two users with different scopes issuing the same SQL would
share a cache entry, leaking data.

This test pins the fix on two layers:

1. ``_make_fingerprint`` joins fields with the unit-separator so
   ("ab","cd","") cannot collide with ("a","bcd","").
2. ``compute_cache_key`` produces different fingerprints for the same
   normalized SQL when identity_role or mapped_pg_role differs, even
   when source_id is identical.
"""

from __future__ import annotations

from interlock.core.normalizer import compute_cache_key, normalize_sql


def test_p0_c_fingerprint_separator_prevents_prefix_collision() -> None:
    """The pre-fix concatenation made ('ab','cd') and ('a','bcd') collide."""
    a = normalize_sql("SELECT 1", "ab", role_context="cd")
    b = normalize_sql("SELECT 1", "a", role_context="bcd")
    assert a.fingerprint != b.fingerprint, (
        "Field delimiter (P0-C) must prevent two source/role triples "
        "from colliding via prefix mashing"
    )


def test_p0_c_role_context_changes_fingerprint() -> None:
    a = normalize_sql("SELECT * FROM customers", "src1", role_context="reader")
    b = normalize_sql("SELECT * FROM customers", "src1", role_context="admin")
    assert a.fingerprint != b.fingerprint


def test_p0_c_compute_cache_key_includes_identity_role() -> None:
    base = "SELECT * FROM customers"
    k_alice = compute_cache_key("src1", base, identity_role="reader", protocol="postgresql")
    k_bob = compute_cache_key("src1", base, identity_role="admin", protocol="postgresql")
    assert k_alice != k_bob


def test_p0_c_compute_cache_key_includes_mapped_pg_role() -> None:
    base = "SELECT * FROM customers"
    k_a = compute_cache_key("src1", base, mapped_pg_role="onyx_reader", protocol="postgresql")
    k_b = compute_cache_key("src1", base, mapped_pg_role="onyx_admin", protocol="postgresql")
    assert k_a != k_b


def test_p0_c_compute_cache_key_includes_tenant() -> None:
    base = "SELECT * FROM customers"
    k_a = compute_cache_key("src1", base, tenant_id="tenant-A", protocol="postgresql")
    k_b = compute_cache_key("src1", base, tenant_id="tenant-B", protocol="postgresql")
    assert k_a != k_b


def test_p0_c_compute_cache_key_includes_policy_scope() -> None:
    base = "SELECT * FROM customers"
    k_a = compute_cache_key("src1", base, policy_scope_hash="hash-a", protocol="postgresql")
    k_b = compute_cache_key("src1", base, policy_scope_hash="hash-b", protocol="postgresql")
    assert k_a != k_b


def test_p0_c_compute_cache_key_includes_literal_parameters() -> None:
    base = "SELECT * FROM customers WHERE id = $1"
    k_a = compute_cache_key("src1", base, parameters=[1], protocol="postgresql")
    k_b = compute_cache_key("src1", base, parameters=[2], protocol="postgresql")
    assert k_a != k_b


def test_p0_c_compute_cache_key_includes_grants_version() -> None:
    base = "SELECT * FROM customers"
    k_a = compute_cache_key("src1", base, grants_version="grant-v1", protocol="postgresql")
    k_b = compute_cache_key("src1", base, grants_version="grant-v2", protocol="postgresql")
    assert k_a != k_b


def test_p0_c_normalized_literal_reads_no_longer_share_cache_key() -> None:
    a = normalize_sql("SELECT * FROM customers WHERE id = 1", "src1")
    b = normalize_sql("SELECT * FROM customers WHERE id = 2", "src1")
    assert not isinstance(a, list)
    assert not isinstance(b, list)
    assert a.normalized_sql == b.normalized_sql
    k_a = compute_cache_key(
        "src1", a.normalized_sql or "", parameters=a.parameters, protocol="postgresql"
    )
    k_b = compute_cache_key(
        "src1", b.normalized_sql or "", parameters=b.parameters, protocol="postgresql"
    )
    assert k_a != k_b


def test_p0_c_pg_proxy_threads_role_context_to_normalize_sql() -> None:
    """Pin the pg_proxy invocation: identity-derived role_context is used."""
    import inspect

    from interlock.gateway import pg_proxy as pgmod

    src = inspect.getsource(pgmod.PGProxy._handle_simple_query)
    assert (
        "role_context=role_context" in src
    ), "pg_proxy must pass role_context derived from identity to normalize_sql (P0-C regression)"
    assert (
        "compute_cache_key(" in src
    ), "pg_proxy must use compute_cache_key with identity context for cache I/O (P0-C regression)"
