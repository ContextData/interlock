"""A cached answer is audited as what it is: redacted when it was cached.

The rc.13 independent evaluation saw MCP cache hits returning
`[REDACTED:EMAIL]` while their audit rows said `pii_detected=False`: no
scanner ran on the hit, so nothing was recorded. The redaction applied when
the answer was cached now travels with the entry, in both cache tiers, and
every protocol copies it into the audit row of a hit.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.cache.l1 import L1Cache
from interlock.cache.provenance import redaction_of, with_redaction
from interlock.cache.strategy import DeterministicFirstStrategy
from interlock.models import CacheResult

REDACTED = with_redaction(
    {"source_id": "shop", "protocol": "mcp"},
    pii_detected=True,
    pii_types=["EMAIL", "EMAIL"],
    redaction_stats={"EMAIL": 3},
)


def test_redaction_round_trips_through_metadata() -> None:
    assert redaction_of(REDACTED) == (True, ["EMAIL"], {"EMAIL": 3})
    assert REDACTED["source_id"] == "shop", "the rest of the metadata is kept"


def test_an_entry_without_a_record_reads_as_none_recorded() -> None:
    """Entries written by an older build carry nothing; they read as before."""
    assert redaction_of({"source_id": "shop"}) == (False, [], {})
    assert redaction_of(None) == (False, [], {})


@pytest.mark.asyncio
async def test_l1_keeps_the_metadata_an_entry_was_stored_with() -> None:
    l1 = L1Cache(max_size=10, ttl_seconds=60)
    await l1.put("k", b"[]", REDACTED)
    result = await l1.get("k")
    assert result.hit and result.data == b"[]"
    assert redaction_of(result.metadata) == (True, ["EMAIL"], {"EMAIL": 3})


@pytest.mark.asyncio
async def test_l1_without_metadata_still_works() -> None:
    l1 = L1Cache(max_size=10, ttl_seconds=60)
    await l1.put("k", b"x")
    assert (await l1.get("k")).metadata == {}


@pytest.mark.asyncio
async def test_promotion_from_l2_keeps_the_metadata() -> None:
    l1 = L1Cache(max_size=10, ttl_seconds=60)
    l2 = MagicMock()
    l2.get = AsyncMock(return_value=CacheResult(hit=True, data=b"[]", tier="l2", metadata=REDACTED))
    strategy = DeterministicFirstStrategy(l1=l1, l2=l2)

    await strategy.get("k")

    promoted = await l1.get("k")
    assert redaction_of(promoted.metadata)[0] is True


@pytest.mark.asyncio
async def test_a_postgresql_hit_is_audited_with_the_cached_redaction() -> None:
    from interlock.gateway.pg_proxy import PGProxy

    proxy = PGProxy(listen_port=0, upstream_port=0)
    cached = b"C\x00\x00\x00\rSELECT 1\x00Z\x00\x00\x00\x05I"
    proxy._cache_get = AsyncMock(  # type: ignore[method-assign]
        return_value=(cached, "l2", with_redaction({}, pii_detected=True, pii_types=["EMAIL"]))
    )
    proxy._emit_audit = AsyncMock()  # type: ignore[method-assign]
    writer = MagicMock()
    writer.drain = AsyncMock()

    await proxy._handle_simple_query(b"SELECT 1\x00", writer, MagicMock(), MagicMock())

    audit = proxy._emit_audit.await_args.kwargs
    assert audit["cache_hit"] is True
    assert audit["pii_detected"] is True
    assert audit["pii_types"] == ["EMAIL"]


@pytest.mark.asyncio
async def test_an_mcp_hit_is_audited_with_the_cached_redaction() -> None:
    from starlette.requests import Request

    from interlock.gateway.mcp_adapter import MCPAdapter
    from interlock.models import IdentityContext

    strategy = AsyncMock()
    strategy.get.return_value = CacheResult(
        hit=True, data=b'[{"email": "[REDACTED:EMAIL]"}]', tier="l1", metadata=REDACTED
    )
    state = SimpleNamespace(
        cache_strategy=strategy,
        audit_logger=AsyncMock(),
        write_classifier=None,
        approval_queue=None,
        pii_scanner=None,
        policy_engine=None,
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/tools/call",
        "headers": [],
        "query_string": b"",
        "app": SimpleNamespace(state=state),
        "state": {},
    }
    request = Request(scope)
    request.state.identity = IdentityContext(
        identity_id=1, user="alice", agent_type="claude_code", roles=["read"]
    )
    adapter = MCPAdapter(pg_pool=AsyncMock())
    adapter._safe_audit = AsyncMock()  # type: ignore[method-assign]

    response = await adapter._execute_query(
        request, {"source_id": "shop", "sql": "SELECT email FROM customers"}
    )

    assert response.status_code == 200
    audit = adapter._safe_audit.await_args.kwargs
    assert audit["cache_hit"] is True
    assert audit["pii_detected"] is True
    assert audit["pii_types"] == ["EMAIL"]
    assert audit["redaction_stats"] == {"EMAIL": 3}
