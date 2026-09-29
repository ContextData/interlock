"""Tests for Phase 2-3 wiring: PII, circuit breaker, cache strategy, invalidation."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interlock.connections.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry
from interlock.connections.manager import ConnectionManager
from interlock.errors import DataSourceUnavailableError
from interlock.gateway.pg_messages import pack_message
from interlock.gateway.pg_proxy import PGProxy
from interlock.models import CacheResult, NormalizedQuery
from interlock.pipeline.pii_fast import PIIFastScanner

# ---------------------------------------------------------------------------
# Helpers: build fake PG DataRow bytes
# ---------------------------------------------------------------------------


def _build_data_row(fields: list[str | None]) -> bytes:
    """Build a PG DataRow message ('D') from a list of text field values.

    None values produce NULL (-1 length).
    """
    body = struct.pack(">H", len(fields))
    for field in fields:
        if field is None:
            body += struct.pack(">i", -1)
        else:
            encoded = field.encode("utf-8")
            body += struct.pack(">i", len(encoded))
            body += encoded
    return pack_message("D", body)


def _build_ready_for_query() -> bytes:
    return pack_message("Z", b"I")


def _build_command_complete(tag: str = "SELECT 1") -> bytes:
    return pack_message("C", tag.encode("utf-8") + b"\x00")


# ---------------------------------------------------------------------------
# Task 1: PII scanning on response bytes
# ---------------------------------------------------------------------------


class TestPIIScanning:
    """Test PII detection in PG proxy response bytes."""

    def test_scan_detects_ssn_in_data_row(self):
        """DataRow containing an SSN should be detected."""
        scanner = PIIFastScanner()
        proxy = PGProxy(pii_scanner=scanner)

        row = _build_data_row(["John Doe", "123-45-6789", "test@example.com"])
        rfq = _build_ready_for_query()
        cmd = _build_command_complete()
        response = cmd + row + rfq

        pii_detected, pii_types = proxy._scan_response_for_pii(response)

        assert pii_detected is True
        assert "SSN" in pii_types
        assert "EMAIL" in pii_types

    def test_scan_no_pii(self):
        """DataRow with no PII should return False."""
        scanner = PIIFastScanner()
        proxy = PGProxy(pii_scanner=scanner)

        row = _build_data_row(["hello", "world"])
        rfq = _build_ready_for_query()
        response = row + rfq

        pii_detected, pii_types = proxy._scan_response_for_pii(response)

        assert pii_detected is False
        assert pii_types == []

    def test_scan_handles_null_fields(self):
        """NULL fields in DataRow should not cause errors."""
        scanner = PIIFastScanner()
        proxy = PGProxy(pii_scanner=scanner)

        row = _build_data_row([None, "999-88-7777", None])
        rfq = _build_ready_for_query()
        response = row + rfq

        pii_detected, pii_types = proxy._scan_response_for_pii(response)

        assert pii_detected is True
        assert "SSN" in pii_types

    def test_scan_without_scanner_returns_false(self):
        """When no PII scanner is set, should return (False, [])."""
        proxy = PGProxy(pii_scanner=None)
        row = _build_data_row(["123-45-6789"])
        pii_detected, pii_types = proxy._scan_response_for_pii(row)

        assert pii_detected is False
        assert pii_types == []


# ---------------------------------------------------------------------------
# Task 2: Circuit breaker blocks when open
# ---------------------------------------------------------------------------


class TestCircuitBreakerWiring:
    """Test circuit breaker integration with ConnectionManager."""

    @pytest.mark.asyncio
    async def test_circuit_open_blocks_get_pool(self):
        """When circuit is open, get_pool should raise DataSourceUnavailableError."""
        registry = MagicMock()
        cb_registry = CircuitBreakerRegistry()

        # Force the breaker to open state
        breaker = cb_registry.get("test_source")
        breaker._state = CircuitBreaker.__dict__  # not what we want
        # Directly set state via internal mechanism
        import time

        from interlock.connections.circuit_breaker import CircuitState

        breaker._state = CircuitState.OPEN
        breaker._opened_at = time.monotonic()  # just opened, not recoverable yet

        manager = ConnectionManager(registry, circuit_breakers=cb_registry)

        with pytest.raises(DataSourceUnavailableError, match="Circuit open"):
            await manager.get_pool("test_source")

    @pytest.mark.asyncio
    async def test_circuit_closed_allows_get_pool(self):
        """When circuit is closed, get_pool should proceed normally."""
        # Create a mock registry that returns a config
        mock_config = MagicMock()
        mock_config.connection_string = "postgresql://localhost/test"
        mock_config.enabled = True
        mock_config.source_type.value = "postgresql"
        mock_config.metadata = {}
        mock_config.connection_config = {}
        mock_config.host = None
        mock_config.port = None
        mock_config.database = None

        registry = MagicMock()
        registry.get.return_value = mock_config

        cb_registry = CircuitBreakerRegistry()
        manager = ConnectionManager(registry, circuit_breakers=cb_registry)

        # Breaker is closed by default. Patch asyncpg.create_pool
        mock_pool = AsyncMock()
        with patch(
            "interlock.connections.manager.asyncpg.create_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            pool = await manager.get_pool("test_source")
            assert pool == mock_pool

    @pytest.mark.asyncio
    async def test_circuit_records_failure_on_pool_error(self):
        """When pool creation fails, circuit breaker should record failure."""
        mock_config = MagicMock()
        mock_config.connection_string = "postgresql://localhost/test"
        mock_config.enabled = True
        mock_config.source_type.value = "postgresql"
        mock_config.metadata = {}
        mock_config.connection_config = {}
        mock_config.host = None
        mock_config.port = None
        mock_config.database = None

        registry = MagicMock()
        registry.get.return_value = mock_config

        cb_registry = CircuitBreakerRegistry()
        manager = ConnectionManager(registry, circuit_breakers=cb_registry)

        with (
            patch(
                "interlock.connections.manager.asyncpg.create_pool",
                side_effect=ConnectionError("refused"),
            ),
            pytest.raises(ConnectionError),
        ):
            await manager.get_pool("fail_source")

        # Breaker should have recorded the failure
        breaker = cb_registry.get("fail_source")
        assert len(breaker._window) == 1
        # The entry should be a failure
        assert breaker._window[0][1] is False


# ---------------------------------------------------------------------------
# Task 3: Cache strategy used when set
# ---------------------------------------------------------------------------


class TestCacheStrategyWiring:
    """Test adaptive cache strategy integration with PG proxy."""

    @pytest.mark.asyncio
    async def test_cache_strategy_get_called(self):
        """When cache_strategy is set, it should be used for lookups."""
        cached_response = _build_command_complete() + _build_ready_for_query()
        mock_strategy = AsyncMock()
        mock_strategy.get.return_value = CacheResult(
            hit=True,
            data=cached_response,
            tier="semantic",
        )

        proxy = PGProxy(cache_strategy=mock_strategy)

        client_writer = MagicMock()
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()

        upstream_reader = AsyncMock()
        upstream_writer = MagicMock()
        upstream_writer.write = MagicMock()
        upstream_writer.drain = AsyncMock()

        sql = b"SELECT 1\x00"

        nq = NormalizedQuery(
            original_sql="SELECT 1",
            fingerprint="abc123",
            tables=[],
            operation="SELECT",
        )

        with patch("interlock.gateway.pg_proxy.normalize_sql", return_value=nq):
            await proxy._handle_simple_query(
                sql,
                client_writer,
                upstream_reader,
                upstream_writer,
            )

        # Strategy should have been called
        mock_strategy.get.assert_called_once()
        # Client should have received the cached bytes
        client_writer.write.assert_called_with(cached_response)

    @pytest.mark.asyncio
    async def test_cache_strategy_put_on_miss(self):
        """On cache miss, cache_strategy.put should be called."""
        mock_strategy = AsyncMock()
        mock_strategy.get.return_value = CacheResult(hit=False)

        proxy = PGProxy(cache_strategy=mock_strategy)

        client_writer = MagicMock()
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()

        # Mock upstream: returns a ReadyForQuery after query
        response_msgs = [
            _build_command_complete(),
            _build_ready_for_query(),
        ]
        msg_index = 0

        async def mock_read_message(reader, **_kwargs):
            """Yield messages from response_msgs one at a time."""
            nonlocal msg_index
            if msg_index >= len(response_msgs):
                raise asyncio.IncompleteReadError(b"", 5)

            raw = response_msgs[msg_index]
            msg_index += 1
            msg_type = chr(raw[0])
            msg_len = struct.unpack(">I", raw[1:5])[0]
            payload = raw[5 : 1 + msg_len]
            return msg_type, payload

        upstream_reader = AsyncMock()
        upstream_writer = MagicMock()
        upstream_writer.write = MagicMock()
        upstream_writer.drain = AsyncMock()

        sql = b"SELECT 1\x00"

        # P0-C: pg_proxy now derives the cache key via compute_cache_key
        # using identity-scoped fields, not the bare nq.fingerprint.
        # We set normalized_sql so the derived key is deterministic.
        nq = NormalizedQuery(
            original_sql="SELECT 1",
            normalized_sql="SELECT $1",
            fingerprint="abc123",
            tables=[],
            operation="SELECT",
        )

        from interlock.core.normalizer import compute_cache_key
        from interlock.gateway.pg_proxy import _decision_scope_hash
        from interlock.gateway.pipeline import GatewayDecision

        expected_key = compute_cache_key(
            source_id=proxy.normalizer_source_id,
            normalized_sql="SELECT $1",
            parameters=nq.parameters,
            policy_scope_hash=_decision_scope_hash(
                GatewayDecision(allowed=True, redaction_required=True)
            ),
        )

        with (
            patch("interlock.gateway.pg_proxy.normalize_sql", return_value=nq),
            patch("interlock.gateway.pg_proxy.read_message", side_effect=mock_read_message),
        ):
            await proxy._handle_simple_query(
                sql,
                client_writer,
                upstream_reader,
                upstream_writer,
            )

        # Strategy put should have been called with the derived key.
        mock_strategy.put.assert_called_once()
        call_args = mock_strategy.put.call_args
        assert call_args[0][0] == expected_key


# ---------------------------------------------------------------------------
# Task 4: Cache invalidation on write execution
# ---------------------------------------------------------------------------


class TestCacheInvalidationWiring:
    """Test cache invalidation when ApprovalQueue executes a write."""

    @pytest.mark.asyncio
    async def test_invalidation_called_on_approved_write(self):
        """After executing an approved write, cache invalidation should fire."""
        from interlock.core.approval_queue import ApprovalQueue

        # Mock pg_pool
        mock_pool = AsyncMock()
        mock_pool.execute.return_value = "UPDATE 1"
        mock_pool.fetchrow.return_value = {
            "id": 1,
            "identity_id": 1,
            "source_id": "src1",
            "sql_text": "DELETE FROM users WHERE id = 5",
            "risk_level": "high",
            "status": "approved",
            "approved_by": "admin",
            "executed_at": None,
            "expires_at": None,
            "request_metadata": "{}",
            "created_at": None,
        }

        # Mock connection manager
        mock_conn_pool = AsyncMock()
        mock_conn_mgr = AsyncMock()
        mock_conn_mgr.get_pool.return_value = mock_conn_pool

        # Mock cache invalidator
        mock_invalidator = AsyncMock()
        mock_invalidator.invalidate_for_tables.return_value = 5

        queue = ApprovalQueue(
            mock_pool,
            connection_manager=mock_conn_mgr,
            cache_invalidator=mock_invalidator,
        )

        result = await queue.approve(1, "admin")

        assert result["executed"] is True
        # Cache invalidation should have been called with the extracted table
        mock_invalidator.invalidate_for_tables.assert_called_once()
        call_args = mock_invalidator.invalidate_for_tables.call_args
        assert call_args[0][0] == "src1"
        assert "users" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_invalidation_for_source_when_no_tables(self):
        """If table extraction fails, invalidate_for_source should be called."""
        from interlock.core.approval_queue import ApprovalQueue

        mock_pool = AsyncMock()
        mock_pool.execute.return_value = "UPDATE 1"
        mock_pool.fetchrow.return_value = {
            "id": 1,
            "identity_id": 1,
            "source_id": "src1",
            "sql_text": "EXECUTE some_procedure()",
            "risk_level": "high",
            "status": "approved",
            "approved_by": "admin",
            "executed_at": None,
            "expires_at": None,
            "request_metadata": "{}",
            "created_at": None,
        }

        mock_conn_pool = AsyncMock()
        mock_conn_mgr = AsyncMock()
        mock_conn_mgr.get_pool.return_value = mock_conn_pool

        mock_invalidator = AsyncMock()
        mock_invalidator.invalidate_for_source.return_value = 3

        queue = ApprovalQueue(
            mock_pool,
            connection_manager=mock_conn_mgr,
            cache_invalidator=mock_invalidator,
        )

        result = await queue.approve(1, "admin")

        assert result["executed"] is True
        # Should fall back to invalidate_for_source since no tables extracted
        mock_invalidator.invalidate_for_source.assert_called_once_with("src1")


# ---------------------------------------------------------------------------
# Table extraction helper
# ---------------------------------------------------------------------------


class TestExtractTablesFromSQL:
    """Test the _extract_tables_from_sql helper."""

    def test_delete_from(self):
        from interlock.core.approval_queue import _extract_tables_from_sql

        tables = _extract_tables_from_sql("DELETE FROM users WHERE id = 5")
        assert tables == ["users"]

    def test_insert_into(self):
        from interlock.core.approval_queue import _extract_tables_from_sql

        tables = _extract_tables_from_sql("INSERT INTO orders (id, amount) VALUES (1, 100)")
        assert tables == ["orders"]

    def test_update(self):
        from interlock.core.approval_queue import _extract_tables_from_sql

        tables = _extract_tables_from_sql("UPDATE accounts SET balance = 0")
        assert tables == ["accounts"]

    def test_no_match(self):
        from interlock.core.approval_queue import _extract_tables_from_sql

        tables = _extract_tables_from_sql("EXECUTE my_proc()")
        assert tables == []
