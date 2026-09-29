"""Audit integrity: the row must be true, not merely present.

Phase 4 of the governance audit, aimed at the surface that actually lied.

When the Write Safety defect shipped, the audit trail said a write had been
approved. The console rendered that as a completed write. Nothing had happened.
Every existing audit test asks whether a row exists and whether its fields are
internally consistent - which that row was. None asked the only question that
matters for an audit trail:

    does the row's account of the world match the world?

So these tests pair each assertion about a row with a reading of the upstream
it describes. A row saying a write succeeded is only accepted if the upstream
actually changed; a row saying a request was denied is only accepted if the
upstream did not.

Every query is bounded twice: by an id captured once the audit buffer has
settled, and by the identity acting. An unbounded "most recent denied row"
query is satisfied by any denial from any earlier run, which would let a
mis-recorded request pass unnoticed. The settling matters just as much -
audit writes are buffered, so rows belonging to earlier requests can be
assigned ids after the bound is taken, and the first version of these tests
failed against a correct system for exactly that reason.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import pytest

from tests.e2e.support import effects
from tests.e2e.support.clients import http_proxy_request

pytestmark = [pytest.mark.e2e]


async def _quiesce(conn: Any) -> int:
    """Let the audit buffer drain, then take the high-water mark.

    Audit writes are buffered and flushed on an interval, so rows belonging to
    *earlier* requests can be assigned ids after this point. Bounding on an id
    taken without settling first pulls in another test's rows, which is what
    made the first version of these tests fail against a correct system.
    """
    previous = -1
    for _ in range(20):
        current = int(await conn.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
        if current == previous:
            return current
        previous = current
        await asyncio.sleep(0.25)
    return previous


async def _rows_for(
    conn: Any, after_id: int, source_id: str, identity_id: int
) -> list[dict[str, Any]]:
    """Rows this identity produced after the bound, once they have flushed."""
    for _ in range(20):
        rows = await conn.fetch(
            "SELECT id, status, identity_id, source_id, operation, error_message"
            " FROM audit_log WHERE id > $1 AND source_id = $2 AND identity_id = $3"
            " ORDER BY id",
            after_id,
            source_id,
            identity_id,
        )
        if rows:
            return [dict(row) for row in rows]
        await asyncio.sleep(0.25)
    return []


async def _identity_id(conn: Any, name: str) -> int:
    value = await conn.fetchval("SELECT id FROM identities WHERE name = $1", name)
    assert value is not None, f"seeded identity {name!r} not found"
    return int(value)


@pytest.mark.asyncio
async def test_one_governed_request_produces_exactly_one_audit_row(
    e2e_config: Any, control_db: Any
) -> None:
    """Not zero, and not several.

    A missing row loses the record of a governed action. Duplicates are just as
    damaging in the other direction: they inflate every count the console and
    the cost breakdown derive from this table.
    """
    identity_id = await _identity_id(control_db, "e2e-agent")
    before = await _quiesce(control_db)

    response = http_proxy_request(
        e2e_config,
        "GET",
        "json/customer",
        headers={"Authorization": f"Bearer {e2e_config.agent_api_key}"},
    )
    assert response.status_code == 200

    rows = await _rows_for(control_db, before, e2e_config.source_id_http, identity_id)
    assert len(rows) == 1, f"one request produced {len(rows)} audit rows: {[r['id'] for r in rows]}"
    assert rows[0]["status"] == "success"
    assert rows[0]["identity_id"] is not None, "the row does not record who made the request"


@pytest.mark.asyncio
async def test_a_denied_request_is_recorded_as_denied(e2e_config: Any, control_db: Any) -> None:
    """The row must say denied, and must be the row for *this* request.

    Bounding by id is the point. Filtering for "the latest denied row" is
    satisfied by any earlier denial, so a request mis-recorded as success would
    still find a row and the assertion would pass.
    """
    identity_id = await _identity_id(control_db, "e2e-denied-agent")
    before = await _quiesce(control_db)

    response = http_proxy_request(
        e2e_config,
        "GET",
        "json/customer",
        headers={"Authorization": f"Bearer {e2e_config.denied_api_key}"},
    )
    assert response.status_code == 403, f"expected a denial, got {response.status_code}"

    rows = await _rows_for(control_db, before, e2e_config.source_id_http, identity_id)
    assert rows, "a denied request produced no audit row at all"
    assert all(
        row["status"] == "denied" for row in rows
    ), f"a denial was not recorded as denied: {[(r['id'], r['status']) for r in rows]}"
    assert any(row["error_message"] for row in rows), "the row records a denial but not why"


@pytest.mark.asyncio
async def test_a_row_claiming_a_write_succeeded_implies_the_write_happened(
    e2e_config: Any, control_db: Any
) -> None:
    """The inverse guarantee, and the one the shipped defect violated.

    The audit trail asserted an approved write; the upstream row survived. Here
    the claim is only accepted once the origin's own call log confirms the
    request arrived.
    """
    identity_id = await _identity_id(control_db, "e2e-agent")
    before = await _quiesce(control_db)
    probe = f"mutation/audit-probe-{secrets.token_hex(4)}"
    calls_before = len(effects.http_upstream_calls(e2e_config, method="POST", path=probe))

    response = http_proxy_request(
        e2e_config,
        "POST",
        probe,
        headers={"Authorization": f"Bearer {e2e_config.agent_api_key}"},
    )

    rows = await _rows_for(control_db, before, e2e_config.source_id_http, identity_id)
    assert rows, "the write produced no audit row"
    calls_after = len(effects.http_upstream_calls(e2e_config, method="POST", path=probe))

    succeeded = [row for row in rows if row["status"] == "success"]
    if succeeded:
        assert calls_after > calls_before, (
            f"audit row {succeeded[0]['id']} records a successful write, but the origin"
            " never received the request - the trail asserts something that did not happen"
        )
    else:
        assert calls_after == calls_before, (
            "the write was not recorded as successful, yet it still reached the origin:"
            f" {calls_before} -> {calls_after}"
        )
    assert response.status_code in (200, 202, 403)


@pytest.mark.asyncio
async def test_a_denied_write_is_recorded_and_left_the_upstream_alone(
    e2e_config: Any, control_db: Any
) -> None:
    """Both halves of a denial: the record says so, and nothing happened."""
    identity_id = await _identity_id(control_db, "e2e-denied-agent")
    before = await _quiesce(control_db)
    probe = f"mutation/audit-denied-{secrets.token_hex(4)}"
    calls_before = len(effects.http_upstream_calls(e2e_config, method="DELETE", path=probe))

    response = http_proxy_request(
        e2e_config,
        "DELETE",
        probe,
        headers={"Authorization": f"Bearer {e2e_config.denied_api_key}"},
    )
    assert response.status_code == 403

    rows = await _rows_for(control_db, before, e2e_config.source_id_http, identity_id)
    assert rows and all(
        row["status"] == "denied" for row in rows
    ), f"a denied write was not recorded as denied: {[(r['id'], r['status']) for r in rows]}"

    calls_after = len(effects.http_upstream_calls(e2e_config, method="DELETE", path=probe))
    assert (
        calls_after == calls_before
    ), "the request was denied and recorded as denied, but still reached the origin"
