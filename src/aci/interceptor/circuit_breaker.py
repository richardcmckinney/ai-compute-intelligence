"""
Circuit breaker for fail-open interceptor safety (Patent Spec Section 6.1).

The interceptor operates with a hard timeout budget of 20ms. If enrichment
data is not retrieved within 20ms from the in-memory attribution index,
the request proceeds without enrichment (fail-open).

This circuit breaker is SYNCHRONOUS by design: the interceptor's hot path
must never await, because the entire enrichment pipeline operates on O(1)
in-memory lookups. The circuit breaker is checked and updated inline with
no locks required (single-threaded per worker; Uvicorn forks workers, each
gets its own interceptor instance).

Three states:
- CLOSED: Normal operation, requests flow through enrichment path.
- OPEN: Enrichment is failing; all requests bypass to fail-open path immediately.
- HALF_OPEN: Testing recovery; limited probe requests test enrichment.
"""

from __future__ import annotations

import time
from enum import StrEnum

import structlog

logger = structlog.get_logger()


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Fail-open circuit breaker for the decision-time interceptor.

    Design invariant: when the circuit opens, ALL requests proceed
    unmodified to the model provider. The interceptor never blocks
    a customer's inference request due to platform failure.

    Synchronous: no locks, no awaits. Each Uvicorn worker process has
    its own interceptor instance; the circuit breaker is not shared
    across workers and does not need thread safety.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        half_open_max_probes: int = 3,
    ) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count: int = 0
        self._failure_threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._half_open_max_probes = half_open_max_probes
        self._last_failure_time: float = 0.0
        self._half_open_success_count: int = 0

        # Counters for monitoring.
        self.total_opens: int = 0
        self.total_fail_opens: int = 0

    @property
    def state(self) -> str:
        """Current circuit state, accounting for automatic half-open transition."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._reset_timeout_s:
                return CircuitState.HALF_OPEN.value
        return self._state.value

    @property
    def is_open(self) -> bool:
        """Check whether the circuit is open (requests should bypass enrichment)."""
        current = self.state
        if current == CircuitState.OPEN.value:
            self.total_fail_opens += 1
            return True
        if current == CircuitState.HALF_OPEN.value:
            # Allow limited probes.
            if self._half_open_success_count < self._half_open_max_probes:
                return False
            return True
        return False

    def record_success(self) -> None:
        """Record a successful enrichment operation."""
        if self._state == CircuitState.HALF_OPEN or self.state == CircuitState.HALF_OPEN.value:
            self._half_open_success_count += 1
            if self._half_open_success_count >= self._half_open_max_probes:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._half_open_success_count = 0
                logger.info("circuit_breaker.closed", reason="probes_succeeded")
        else:
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record a failed enrichment operation."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN or self.state == CircuitState.HALF_OPEN.value:
            self._state = CircuitState.OPEN
            self._half_open_success_count = 0
            self.total_opens += 1
            logger.warning("circuit_breaker.reopened", reason="probe_failed")
        elif self._failure_count >= self._failure_threshold:
            self._state = CircuitState.OPEN
            self._half_open_success_count = 0
            self.total_opens += 1
            logger.warning(
                "circuit_breaker.opened",
                failures=self._failure_count,
                threshold=self._failure_threshold,
            )

    def force_open(self) -> None:
        """Force the circuit open (e.g., during maintenance)."""
        self._state = CircuitState.OPEN
        self._last_failure_time = time.monotonic()
        self.total_opens += 1
        logger.info("circuit_breaker.force_opened")

    def force_close(self) -> None:
        """Force the circuit closed (e.g., after manual recovery)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_success_count = 0
        logger.info("circuit_breaker.force_closed")

    def get_metrics(self) -> dict:
        """Return circuit breaker metrics for monitoring."""
        return {
            "state": self.state,
            "failure_count": self._failure_count,
            "failure_threshold": self._failure_threshold,
            "total_opens": self.total_opens,
            "total_fail_opens": self.total_fail_opens,
        }
