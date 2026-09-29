"""Granting and revoking must not be able to create an inert grant.

The SQL semantics - idempotency, soft revoke, the unique constraint - are
proven against a real PostgreSQL in tests/e2e/test_grant_lifecycle.py, because
asserting them against a fake would only prove the fake. What is tested here is
the part that is Python: which role a grant resolves to, and which requests are
refused outright.
"""

from __future__ import annotations

from typing import Any

import pytest

from interlock.core.source_role_grants import (
    GrantError,
    RoleSourceMismatchError,
    UnknownRoleError,
    grant,
    resolve_role,
)


class _Pool:
    """Answers role lookups from a small table and records the grant written."""

    def __init__(self, roles: list[dict[str, Any]]) -> None:
        self._roles = roles
        self.granted: tuple[Any, ...] | None = None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "FROM source_roles" in sql:
            if "role_key = lower" in sql:
                source_id, role_key = args
                return next(
                    (
                        r
                        for r in self._roles
                        if r["source_id"] == source_id and r["role_key"] == role_key.lower()
                    ),
                    None,
                )
            (role_id,) = args
            return next((r for r in self._roles if r["id"] == role_id), None)
        self.granted = args
        return {
            "id": 99,
            "identity_id": args[0],
            "source_id": args[1],
            "role_id": args[2],
            "enabled": True,
            "expires_at": args[3],
            "granted_by": args[4],
            "created_at": None,
            "updated_at": None,
        }


def _pool() -> _Pool:
    return _Pool(
        [
            {"id": 1, "role_key": "reader", "source_id": "sales_pg"},
            {"id": 2, "role_key": "reader", "source_id": "hr_mysql"},
        ]
    )


@pytest.mark.asyncio
async def test_a_role_from_another_source_is_refused() -> None:
    """The failure this check exists to prevent is silent.

    The read path joins `source_roles` on role_id *and* source_id, so a grant
    carrying another source's role is stored happily and then never matches.
    It reads as a successful grant and behaves as no grant at all.
    """
    with pytest.raises(RoleSourceMismatchError, match="hr_mysql"):
        await resolve_role(_pool(), source_id="sales_pg", role_id=2)


@pytest.mark.asyncio
async def test_a_role_key_resolves_within_its_own_source() -> None:
    """The same key exists on both sources; the grant must pick the right one."""
    assert await resolve_role(_pool(), source_id="hr_mysql", role_key="reader") == (2, "reader")
    assert await resolve_role(_pool(), source_id="sales_pg", role_key="reader") == (1, "reader")


@pytest.mark.asyncio
async def test_an_unknown_role_is_refused_rather_than_stored() -> None:
    with pytest.raises(UnknownRoleError):
        await resolve_role(_pool(), source_id="sales_pg", role_key="nonexistent")
    with pytest.raises(UnknownRoleError):
        await resolve_role(_pool(), source_id="sales_pg", role_id=404)


@pytest.mark.asyncio
async def test_a_grant_naming_no_role_at_all_is_refused() -> None:
    with pytest.raises(GrantError, match="role_key or a role_id"):
        await resolve_role(_pool(), source_id="sales_pg")


@pytest.mark.asyncio
async def test_the_acting_administrator_is_recorded_on_the_grant() -> None:
    """`granted_by` had never been written anywhere, so "who gave this agent
    access, and when" was unanswerable."""
    pool = _pool()

    result = await grant(
        pool, identity_id=7, source_id="sales_pg", role_key="reader", granted_by=42
    )

    assert pool.granted is not None
    assert pool.granted[4] == 42
    assert result.granted_by == 42
    assert result.role_key == "reader"


@pytest.mark.asyncio
async def test_a_grant_resolves_the_role_before_writing_anything() -> None:
    """A refused grant must leave no row behind."""
    pool = _pool()

    with pytest.raises(RoleSourceMismatchError):
        await grant(pool, identity_id=7, source_id="sales_pg", role_id=2)

    assert pool.granted is None
