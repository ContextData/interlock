"""Unit tests for the circuit breaker."""

from __future__ import annotations

import time

from interlock.connections.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
)


class TestCircuitBreakerInitialState:
    """Basic state tests."""

    def test_initial_state_is_closed(self) -> None:
        cb = CircuitBreaker(source_id="test")
        assert cb.state == CircuitState.CLOSED

    def test_can_execute_when_closed(self) -> None:
        cb = CircuitBreaker(source_id="test")
        assert cb.can_execute() is True

    def test_record_success_keeps_closed(self) -> None:
        cb = CircuitBreaker(source_id="test")
        for _ in range(20):
            cb.record_success()
        assert cb.state == CircuitState.CLOSED


class TestSlidingWindow:
    """Sliding window error rate detection."""

    def test_opens_on_high_error_rate(self) -> None:
        cb = CircuitBreaker(
            source_id="test",
            window_seconds=60.0,
            error_threshold_pct=50.0,
            min_requests=10,
        )
        # Record 5 successes then 6 failures (11 total, >50% errors)
        for _ in range(5):
            cb.record_success()
        for _ in range(6):
            cb.record_failure()

        assert cb.state == CircuitState.OPEN

    def test_stays_closed_below_threshold(self) -> None:
        cb = CircuitBreaker(
            source_id="test",
            window_seconds=60.0,
            error_threshold_pct=50.0,
            min_requests=10,
        )
        # 8 successes, 3 failures = 27% error rate
        for _ in range(8):
            cb.record_success()
        for _ in range(3):
            cb.record_failure()

        assert cb.state == CircuitState.CLOSED

    def test_stays_closed_below_min_requests(self) -> None:
        cb = CircuitBreaker(
            source_id="test",
            window_seconds=60.0,
            error_threshold_pct=50.0,
            min_requests=10,
        )
        # All failures but only 5 requests (below min_requests=10)
        for _ in range(5):
            cb.record_failure()

        assert cb.state == CircuitState.CLOSED


class TestPoolExhaustion:
    """Pool exhaustion fast-fail detection."""

    def test_immediately_opens(self) -> None:
        cb = CircuitBreaker(source_id="test")
        assert cb.state == CircuitState.CLOSED
        cb.record_pool_exhaustion()
        assert cb.state == CircuitState.OPEN

    def test_rejects_after_pool_exhaustion(self) -> None:
        cb = CircuitBreaker(source_id="test", recovery_timeout=9999.0)
        cb.record_pool_exhaustion()
        assert cb.can_execute() is False


class TestHealthCheck:
    """Health check heartbeat detection."""

    def test_three_consecutive_failures_opens(self) -> None:
        cb = CircuitBreaker(
            source_id="test",
            consecutive_failures_to_open=3,
        )
        cb.record_health_check_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_health_check_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_health_check_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_counter(self) -> None:
        cb = CircuitBreaker(
            source_id="test",
            consecutive_failures_to_open=3,
        )
        cb.record_health_check_failure()
        cb.record_health_check_failure()
        cb.record_health_check_success()  # reset
        cb.record_health_check_failure()
        cb.record_health_check_failure()
        # Only 2 consecutive, should still be closed
        assert cb.state == CircuitState.CLOSED

    def test_custom_threshold(self) -> None:
        cb = CircuitBreaker(
            source_id="test",
            consecutive_failures_to_open=5,
        )
        for _ in range(4):
            cb.record_health_check_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_health_check_failure()
        assert cb.state == CircuitState.OPEN


class TestOpenState:
    """Tests for OPEN state behavior."""

    def test_rejects_requests(self) -> None:
        cb = CircuitBreaker(source_id="test", recovery_timeout=9999.0)
        cb.record_pool_exhaustion()
        assert cb.can_execute() is False
        assert cb.can_execute() is False


class TestRecoveryAndHalfOpen:
    """Tests for OPEN -> HALF_OPEN -> CLOSED transitions."""

    def test_transitions_to_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker(source_id="test", recovery_timeout=0.05)
        cb.record_pool_exhaustion()
        assert cb.state == CircuitState.OPEN

        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_allows_one_probe(self) -> None:
        cb = CircuitBreaker(source_id="test", recovery_timeout=0.05)
        cb.record_pool_exhaustion()

        time.sleep(0.06)
        # First call should be allowed (probe)
        assert cb.can_execute() is True
        # Second call should be rejected (already probing)
        assert cb.can_execute() is False

    def test_successful_probe_closes_circuit(self) -> None:
        cb = CircuitBreaker(source_id="test", recovery_timeout=0.05)
        cb.record_pool_exhaustion()

        time.sleep(0.06)
        assert cb.can_execute() is True  # probe allowed
        cb.record_success()  # probe succeeds
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True  # back to normal

    def test_failed_probe_reopens_circuit(self) -> None:
        cb = CircuitBreaker(source_id="test", recovery_timeout=0.05)
        cb.record_pool_exhaustion()

        time.sleep(0.06)
        assert cb.can_execute() is True  # probe allowed
        cb.record_failure()  # probe fails
        assert cb.state == CircuitState.OPEN

    def test_reset_returns_to_closed(self) -> None:
        cb = CircuitBreaker(source_id="test", recovery_timeout=9999.0)
        cb.record_pool_exhaustion()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True


class TestCircuitBreakerRegistry:
    """Tests for CircuitBreakerRegistry."""

    def test_creates_breaker_on_first_get(self) -> None:
        registry = CircuitBreakerRegistry()
        cb = registry.get("source-1")
        assert isinstance(cb, CircuitBreaker)
        assert cb.source_id == "source-1"

    def test_reuses_existing_breaker(self) -> None:
        registry = CircuitBreakerRegistry()
        cb1 = registry.get("source-1")
        cb2 = registry.get("source-1")
        assert cb1 is cb2

    def test_different_sources_get_different_breakers(self) -> None:
        registry = CircuitBreakerRegistry()
        cb1 = registry.get("source-1")
        cb2 = registry.get("source-2")
        assert cb1 is not cb2

    def test_get_all_states(self) -> None:
        registry = CircuitBreakerRegistry()
        cb1 = registry.get("source-1")
        cb2 = registry.get("source-2")
        cb2.record_pool_exhaustion()

        states = registry.get_all_states()
        assert states == {
            "source-1": CircuitState.CLOSED,
            "source-2": CircuitState.OPEN,
        }

    def test_get_all_states_empty(self) -> None:
        registry = CircuitBreakerRegistry()
        assert registry.get_all_states() == {}
