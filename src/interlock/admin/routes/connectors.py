"""Read-only connector API: which connectors are active for new sources."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from interlock.connections.activation import connector_rows

router = APIRouter(prefix="/api/connectors", tags=["connectors"])


@router.get("")
async def list_connectors(request: Request) -> list[dict[str, Any]]:
    """Every connector a source can be registered on, with `active` and usage."""
    return await connector_rows(request.app.state.pg_pool)
