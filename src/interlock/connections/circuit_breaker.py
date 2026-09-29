"""Per-data-source circuit breaker with three detection layers.

Detection layers:
  1. Health check heartbeat - 3 consecutive failures opens circuit
  2. Sliding window errors - error rate over window exceeding threshold opens circuit
  3. Pool exhaustion fast-fail - immediately opens circuit
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque


class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker for a single data source.

    Tracks request outcomes via a sliding time window and health-check
    heartbeats.  Transitions:

        CLOSED  -> OPEN       when any detection layer triggers
        OPEN    -> HALF_OPEN  after recovery_timeout elapses
        HALF_OPEN -> CLOSED   on a successful probe request
        HALF_OPEN -> OPEN     on a failed probe request
    """

    def __init__(
        self,
        source_id: str,
        # Sliding window config
        window_seconds: float = 60.0,
        error_threshold_pct: float = 50.0,
        min_requests: int = 10,
        # Recovery config
        recovery_timeout: float = 30.0,
        # Health check config
        consecutive_failures_to_open: int = 3,
        health_check_interval: float = 5.0,
    ) -> None:
        self.source_id = source_id

        # Sliding window parameters
        self._window_seconds = window_seconds
        self._error_threshold_pct = error_threshold_pct
        self._min_requests = min_requests

        # Recovery
        self._recovery_timeout = recovery_timeout

        # Health check
        self._consecutive_failures_to_open = consecutive_failures_to_open
        self._health_check_interval = health_check_interval

        # Internal state
        self._state = CircuitState.CLOSED
        self._opened_at: float = 0.0
        self._half_open_probe_sent = False

        # Sliding window: deque of (timestamp, is_success) tuples
        self._window: deque[tuple[float, bool]] = deque()

        # Health check consecutive failure counter
        self._health_check_failures = 0

        self._lock = threading.Lock()

    # -- Properties -----------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    # -- Recording methods ----------------------------------------------------

    def record_success(self) -> None:
        """Record a successful request."""
        with self._lock:
            now = time.monotonic()
            self._window.append((now, True))
            self._prune_window(now)

            if self._state == CircuitState.HALF_OPEN:
                # Successful probe - close the circuit
                self._state = CircuitState.CLOSED
                self._half_open_probe_sent = False

    def record_failure(self) -> None:
        """Record a failed request. May open circuit via sliding window."""
        with self._lock:
            now = time.monotonic()
            self._window.append((now, False))
            self._prune_window(now)

            if self._state == CircuitState.HALF_OPEN:
                # Failed probe - reopen
                self._open_circuit(now)
                return

            if self._state == CircuitState.CLOSED:
                self._check_sliding_window(now)

    def record_pool_exhaustion(self) -> None:
        """Immediate circuit open on pool exhaustion."""
        with self._lock:
            self._open_circuit(time.monotonic())

    def record_health_check_failure(self) -> None:
        """Record health check failure. N consecutive failures opens circuit."""
        with self._lock:
            self._health_check_failures += 1
            if self._health_check_failures >= self._consecutive_failures_to_open:
                self._open_circuit(time.monotonic())

    def record_health_check_success(self) -> None:
        """Reset health check failure counter."""
        with self._lock:
            self._health_check_failures = 0

    # -- Query methods --------------------------------------------------------

    def can_execute(self) -> bool:
        """Check if a request should be allowed.

        CLOSED: always True
        OPEN: False unless recovery timeout elapsed (transitions to HALF_OPEN)
        HALF_OPEN: allow one probe request
        """
        with self._lock:
            self._maybe_transition_to_half_open()

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.HALF_OPEN:
                if not self._half_open_probe_sent:
                    self._half_open_probe_sent = True
                    return True
                return False

            # OPEN
            return False

    def reset(self) -> None:
        """Force reset to CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._opened_at = 0.0
            self._half_open_probe_sent = False
            self._health_check_failures = 0
            self._window.clear()

    # -- Internals ------------------------------------------------------------

    def _open_circuit(self, now: float) -> None:
        """Transition to OPEN state. Caller must hold the lock."""
        self._state = CircuitState.OPEN
        self._opened_at = now
        self._half_open_probe_sent = False

    def _maybe_transition_to_half_open(self) -> None:
        """If OPEN and recovery timeout has elapsed, go to HALF_OPEN.

        Caller must hold the lock.
        """
        if self._state != CircuitState.OPEN:
            return
        now = time.monotonic()
        if now - self._opened_at >= self._recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._half_open_probe_sent = False

    def _prune_window(self, now: float) -> None:
        """Remove entries older than the window. Caller must hold the lock."""
        cutoff = now - self._window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _check_sliding_window(self, now: float) -> None:
        """Check if error rate exceeds threshold. Caller must hold the lock."""
        self._prune_window(now)
        total = len(self._window)
        if total < self._min_requests:
            return
        errors = sum(1 for _, success in self._window if not success)
        error_pct = (errors / total) * 100.0
        if error_pct >= self._error_threshold_pct:
            self._open_circuit(now)


class CircuitBreakerRegistry:
    """Manages circuit breakers per data source."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, source_id: str, **kwargs: object) -> CircuitBreaker:
        """Get or create a circuit breaker for a source.

        Extra keyword arguments are forwarded to CircuitBreaker() on creation.
        """
        with self._lock:
            if source_id not in self._breakers:
                self._breakers[source_id] = CircuitBreaker(source_id=source_id, **kwargs)  # type: ignore[arg-type]
            return self._breakers[source_id]

    def get_all_states(self) -> dict[str, CircuitState]:
        """Return state of all known breakers."""
        with self._lock:
            return {sid: cb.state for sid, cb in self._breakers.items()}
