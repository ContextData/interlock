"""An approved write must reach its connector in the shape that connector needs.

`_execute_via_adapter` built a request containing only `query`, the SQL text of
the approval. That is right for SQL connectors and wrong for everything else:
object storage needs an operation, an asset reference and a body. S3's
`execute_write` rejects a request without them, and the resulting `ValueError`
fell through to the generic failure path - so an approval that could never have
executed was recorded as a *failed write* with an opaque message, sending an
operator to look for an upstream problem that did not exist.

Two things changed. Structured parameters recorded on the approval are now
forwarded, so a caller that supplies them reaches the connector intact; and a
shape rejection is translated into a refusal that says what is actually wrong.

Note what this does *not* claim: S3 still has no governed write
entry point. It declares supports_query=False and supports_proxy=False, so no
protocol surface can express an object write today. This makes the plumbing
correct and the failure honest; it does not make S3 writes reachable.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from interlock.core.approval_queue import ApprovalQueue, _parse_metadata
from interlock.errors import InterLockError


class _Adapter:
    """Records what the queue handed it, and can reject a request's shape."""

    def __init__(self, *, reject_shape: bool = False) -> None:
        self.reject_shape = reject_shape
        self.received: dict[str, Any] | None = None

    async def execute_write(self, request: dict[str, Any]) -> dict[str, Any]:
        self.received = request
        if self.reject_shape and not request.get("operation"):
            raise ValueError("S3 execute_write supports write/upload/put/delete/remove")
        return {"ok": True}


class _ReadOnlyAdapter:
    async def execute_write(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("writes are intentionally disabled")


class _Source:
    connection_config = {"bucket": "b"}
    metadata: dict[str, Any] = {}


def _row(metadata: Any = None) -> dict[str, Any]:
    return {
        "id": 7,
        "source_id": "src",
        "identity_id": 3,
        "sql_text": "DELETE FROM things",
        "request_metadata": metadata,
    }


@pytest.fixture
def queue() -> ApprovalQueue:
    return ApprovalQueue(pg_pool=None)  # type: ignore[arg-type]


class TestStructuredForwarding:
    @pytest.mark.asyncio
    async def test_structured_parameters_reach_the_connector(
        self, queue: ApprovalQueue, monkeypatch
    ) -> None:
        """The fix: an approval carrying a structured write is not flattened to SQL."""
        adapter = _Adapter(reject_shape=True)
        monkeypatch.setattr("interlock.core.approval_queue.get_adapter", lambda *a, **k: adapter)

        await queue._execute_via_adapter(
            _row({"operation": "delete", "asset_ref": "s3://b/k"}), _Source(), "s3"
        )

        assert adapter.received is not None
        assert adapter.received["operation"] == "delete"
        assert adapter.received["asset_ref"] == "s3://b/k"

    @pytest.mark.asyncio
    async def test_the_statement_is_still_forwarded_for_sql_connectors(
        self, queue: ApprovalQueue, monkeypatch
    ) -> None:
        """MySQL and friends consume `query`; forwarding extras must not disturb them."""
        adapter = _Adapter()
        monkeypatch.setattr("interlock.core.approval_queue.get_adapter", lambda *a, **k: adapter)

        await queue._execute_via_adapter(_row(), _Source(), "mysql")

        assert adapter.received is not None
        assert adapter.received["query"] == "DELETE FROM things"
        assert "operation" not in adapter.received

    @pytest.mark.asyncio
    async def test_metadata_stored_as_a_json_string_is_still_read(
        self, queue: ApprovalQueue, monkeypatch
    ) -> None:
        """Older rows recorded metadata as text rather than as jsonb."""
        adapter = _Adapter(reject_shape=True)
        monkeypatch.setattr("interlock.core.approval_queue.get_adapter", lambda *a, **k: adapter)

        await queue._execute_via_adapter(
            _row(json.dumps({"operation": "put", "body": "x"})), _Source(), "s3"
        )

        assert adapter.received is not None
        assert adapter.received["operation"] == "put"

    @pytest.mark.asyncio
    async def test_absent_metadata_keys_are_not_invented(
        self, queue: ApprovalQueue, monkeypatch
    ) -> None:
        """A None in metadata must not become a None the connector has to handle."""
        adapter = _Adapter()
        monkeypatch.setattr("interlock.core.approval_queue.get_adapter", lambda *a, **k: adapter)

        await queue._execute_via_adapter(_row({"operation": None}), _Source(), "mysql")

        assert adapter.received is not None
        assert "operation" not in adapter.received


class TestHonestFailure:
    @pytest.mark.asyncio
    async def test_a_shape_rejection_says_what_is_wrong(
        self, queue: ApprovalQueue, monkeypatch
    ) -> None:
        """The message an operator actually reads when this fails.

        It used to be a generic failed write, which points at the upstream.
        The upstream was never contacted.
        """
        adapter = _Adapter(reject_shape=True)
        monkeypatch.setattr("interlock.core.approval_queue.get_adapter", lambda *a, **k: adapter)

        with pytest.raises(InterLockError) as excinfo:
            await queue._execute_via_adapter(_row(), _Source(), "s3")

        message = str(excinfo.value)
        assert "structured write request" in message
        assert "no governed write entry point" in message
        assert "s3" in message

    @pytest.mark.asyncio
    async def test_a_read_only_connector_still_refuses_distinctly(
        self, queue: ApprovalQueue, monkeypatch
    ) -> None:
        """Two different refusals must stay distinguishable.

        "cannot write at all" and "needs a different request shape" send an
        operator to different places.
        """
        monkeypatch.setattr(
            "interlock.core.approval_queue.get_adapter", lambda *a, **k: _ReadOnlyAdapter()
        )

        with pytest.raises(InterLockError, match="does not implement writes"):
            await queue._execute_via_adapter(_row(), _Source(), "slack")

    @pytest.mark.asyncio
    async def test_an_upstream_error_is_not_swallowed_as_a_shape_problem(
        self, queue: ApprovalQueue, monkeypatch
    ) -> None:
        """Only ValueError and KeyError mean "wrong shape".

        A genuine upstream failure must keep propagating, or a real outage
        would be reported as a configuration problem.
        """

        class _Failing:
            async def execute_write(self, request: dict[str, Any]) -> dict[str, Any]:
                raise ConnectionError("upstream unreachable")

        monkeypatch.setattr("interlock.core.approval_queue.get_adapter", lambda *a, **k: _Failing())

        with pytest.raises(ConnectionError):
            await queue._execute_via_adapter(_row(), _Source(), "s3")


def test_metadata_parsing_survives_every_stored_shape() -> None:
    assert _parse_metadata(None) == {}
    assert _parse_metadata({"a": 1}) == {"a": 1}
    assert _parse_metadata('{"a": 1}') == {"a": 1}
    assert _parse_metadata("not json") == {}
    assert _parse_metadata(42) == {}
