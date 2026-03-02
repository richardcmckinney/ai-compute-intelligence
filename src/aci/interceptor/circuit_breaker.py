"""
Circuit breaker for fail-open interceptor safety (Patent Spec Section 6.1).

The interceptor operates with a hard timeout budget of 20ms. If enrichment
cannot complete within budget, the request proceeds unmodified (fail-open).

Production requirement: circuit state must be shareable across horizontally
scaled replicas. This module supports:
- local state (development/testing)
- Redis-backed shared state (production)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

import structlog
from redis import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger()


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerState:
    state: str = CircuitState.CLOSED.value
    failure_count: int = 0
    last_failure_time_epoch_s: float = 0.0
    half_open_success_count: int = 0
    half_open_probe_count: int = 0
    total_opens: int = 0
    total_fail_opens: int = 0


class CircuitStateStore(Protocol):
    def load(self) -> CircuitBreakerState:
        ...

    def save(self, state: CircuitBreakerState) -> None:
        ...


class LocalCircuitStateStore:
    """In-process circuit state storage."""

    def __init__(self) -> None:
        self._state = CircuitBreakerState()

    def load(self) -> CircuitBreakerState:
        return CircuitBreakerState(**asdict(self._state))

    def save(self, state: CircuitBreakerState) -> None:
        self._state = CircuitBreakerState(**asdict(state))


class RedisCircuitStateStore:
    """Redis-backed circuit state for cluster-wide fail-open consistency."""

    def __init__(
        self,
        redis_url: str,
        key: str,
        *,
        socket_timeout_s: float = 0.01,
    ) -> None:
        self._redis = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=socket_timeout_s,
            socket_connect_timeout=socket_timeout_s,
        )
        self._key = key

    def load(self) -> CircuitBreakerState:
        raw = self._redis.get(self._key)
        if not raw:
            state = CircuitBreakerState()
            self.save(state)
            return state

        parsed = json.loads(raw)
        return CircuitBreakerState(**parsed)

    def save(self, state: CircuitBreakerState) -> None:
        self._redis.set(self._key, json.dumps(asdict(state)))


class CircuitBreaker:
    """
    Fail-open circuit breaker for the decision-time interceptor.

    If Redis-backed state store is configured, all pods share the same
    circuit state transitions and fail-open behavior.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        half_open_max_probes: int = 3,
        state_store: CircuitStateStore | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._half_open_max_probes = half_open_max_probes
        self._state_store = state_store or LocalCircuitStateStore()
        self._state_store.save(self._state_store.load())

    @property
    def state(self) -> str:
        """Current circuit state with automatic OPEN -> HALF_OPEN transition."""
        state = self._safe_load()
        if state.state == CircuitState.OPEN.value:
            elapsed = time.time() - state.last_failure_time_epoch_s
            if elapsed >= self._reset_timeout_s:
                state.state = CircuitState.HALF_OPEN.value
                state.half_open_success_count = 0
                state.half_open_probe_count = 0
                self._safe_save(state)
        return state.state

    @property
    def is_open(self) -> bool:
        """Check whether the circuit is open and update fail-open counters."""
        current = self.state
        state = self._safe_load()

        if current == CircuitState.OPEN.value:
            state.total_fail_opens += 1
            self._safe_save(state)
            return True

        if current == CircuitState.HALF_OPEN.value:
            if state.half_open_probe_count < self._half_open_max_probes:
                state.half_open_probe_count += 1
                self._safe_save(state)
                return False
            state.total_fail_opens += 1
            self._safe_save(state)
            return True

        return False

    def record_success(self) -> None:
        """Record a successful enrichment operation."""
        current = self.state
        state = self._safe_load()

        if current == CircuitState.HALF_OPEN.value:
            state.half_open_success_count += 1
            if state.half_open_success_count >= self._half_open_max_probes:
                state.state = CircuitState.CLOSED.value
                state.failure_count = 0
                state.half_open_success_count = 0
                state.half_open_probe_count = 0
                logger.info("circuit_breaker.closed", reason="probes_succeeded")
        else:
            state.failure_count = 0
            state.state = CircuitState.CLOSED.value

        self._safe_save(state)

    def record_failure(self) -> None:
        """Record a failed enrichment operation."""
        current = self.state
        state = self._safe_load()

        if current == CircuitState.HALF_OPEN.value:
            state.state = CircuitState.OPEN.value
            state.half_open_success_count = 0
            state.half_open_probe_count = 0
            state.failure_count = self._failure_threshold
            state.last_failure_time_epoch_s = time.time()
            state.total_opens += 1
            self._safe_save(state)
            logger.warning("circuit_breaker.reopened", reason="probe_failed")
            return

        state.failure_count += 1
        state.last_failure_time_epoch_s = time.time()

        if state.failure_count >= self._failure_threshold:
            state.state = CircuitState.OPEN.value
            state.half_open_success_count = 0
            state.half_open_probe_count = 0
            state.total_opens += 1
            logger.warning(
                "circuit_breaker.opened",
                failures=state.failure_count,
                threshold=self._failure_threshold,
            )

        self._safe_save(state)

    def force_open(self) -> None:
        """Force the circuit open (e.g., during maintenance)."""
        state = self._safe_load()
        state.state = CircuitState.OPEN.value
        state.last_failure_time_epoch_s = time.time()
        state.half_open_success_count = 0
        state.half_open_probe_count = 0
        state.total_opens += 1
        self._safe_save(state)
        logger.info("circuit_breaker.force_opened")

    def force_close(self) -> None:
        """Force the circuit closed (e.g., after manual recovery)."""
        state = self._safe_load()
        state.state = CircuitState.CLOSED.value
        state.failure_count = 0
        state.half_open_success_count = 0
        state.half_open_probe_count = 0
        self._safe_save(state)
        logger.info("circuit_breaker.force_closed")

    def get_metrics(self) -> dict:
        """Return circuit breaker metrics for monitoring."""
        state = self._safe_load()
        return {
            "state": self.state,
            "failure_count": state.failure_count,
            "failure_threshold": self._failure_threshold,
            "half_open_probe_count": state.half_open_probe_count,
            "total_opens": state.total_opens,
            "total_fail_opens": state.total_fail_opens,
            "backend": type(self._state_store).__name__,
        }

    def _safe_load(self) -> CircuitBreakerState:
        try:
            return self._state_store.load()
        except RedisError as exc:
            logger.warning("circuit_breaker.state_load_failed", error=str(exc))
            return CircuitBreakerState()

    def _safe_save(self, state: CircuitBreakerState) -> None:
        try:
            self._state_store.save(state)
        except RedisError as exc:
            logger.warning("circuit_breaker.state_save_failed", error=str(exc))
