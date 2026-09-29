"""Unified RequestPipeline scaffold.

Phase 1's audit found that PG, HTTP, and MCP each ran their own copy
of the governance pipeline. This module exposes the shared
abstraction. The protocol adapters can incrementally adopt it instead
of duplicating logic.

Right now the gateway's PG simple-query path and MCP query path both
implement the same conceptual flow:

    authenticate -> authorize -> normalize -> cache_lookup ->
        origin_fetch -> response_process (PII redact) -> cache_write ->
        audit_emit

This module provides:

- ``PipelineContext`` - request-scoped state passed through stages.
- ``Stage`` - protocol with ``async def __call__(ctx)``.
- ``RequestPipeline.run(ctx)`` - execute stages sequentially with
  per-stage timing recorded into ``ctx.stage_latencies``.

The MCP/HTTP/PG adapters will migrate stage-by-stage in a later phase.
The skeleton intentionally has no behaviour beyond timing and stage
ordering - it's just the contract.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class PipelineContext:
    """Per-request state shared across stages."""

    protocol: str  # "pg" | "http" | "mcp"
    operation: str = "read"
    source_id: str | None = None
    sql: str | None = None
    identity: Any | None = None
    fingerprint: str | None = None
    intent_embedding: list[float] | None = None
    intent_text: str | None = None
    cache_tier: str | None = None
    cache_hit: bool = False
    response_bytes: bytes | None = None
    response_rows: list[dict[str, Any]] | None = None
    pii_detected: bool = False
    pii_types: list[str] = field(default_factory=list)
    audit_status: str = "success"
    error_message: str | None = None
    stage_latencies: dict[str, float] = field(default_factory=dict)
    request_metadata: dict[str, Any] = field(default_factory=dict)


class Stage(Protocol):
    name: str

    async def __call__(self, ctx: PipelineContext) -> None: ...


class RequestPipeline:
    """Sequential stage executor with per-stage timing."""

    def __init__(self, stages: list[Stage]) -> None:
        self._stages = list(stages)

    @property
    def stages(self) -> list[Stage]:
        return list(self._stages)

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        for stage in self._stages:
            t0 = time.monotonic()
            try:
                await stage(ctx)
            finally:
                ctx.stage_latencies[stage.name] = (time.monotonic() - t0) * 1000.0
        return ctx


# A thin functional helper so an adapter can compose stages from
# closures without subclassing.
def make_stage(
    name: str,
    fn: Callable[[PipelineContext], Awaitable[None]],
) -> Stage:
    class _F:
        def __init__(self, _name: str, _fn) -> None:
            self.name = _name
            self._fn = _fn

        async def __call__(self, ctx: PipelineContext) -> None:
            await self._fn(ctx)

    return _F(name, fn)
