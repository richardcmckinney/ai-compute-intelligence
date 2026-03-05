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
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

import structlog
from redis import Redis
from redis.exceptions import RedisError, WatchError

logger = structlog.get_logger()

if TYPE_CHECKING:
    from collections.abc import Callable


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
    def load(self) -> CircuitBreakerState: ...

    def save(self, state: CircuitBreakerState) -> None: ...

    def mutate(self, mutator: Callable[[CircuitBreakerState], None]) -> CircuitBreakerState: ...


class LocalCircuitStateStore:
    """In-process circuit state storage."""

    def __init__(self) -> None:
        self._state = CircuitBreakerState()
        self._lock = threading.RLock()

    def load(self) -> CircuitBreakerState:
        with self._lock:
            return CircuitBreakerState(**asdict(self._state))

    def save(self, state: CircuitBreakerState) -> None:
        with self._lock:
            self._state = CircuitBreakerState(**asdict(state))

    def mutate(self, mutator: Callable[[CircuitBreakerState], None]) -> CircuitBreakerState:
        with self._lock:
            state = CircuitBreakerState(**asdict(self._state))
            mutator(state)
            self._state = CircuitBreakerState(**asdict(state))
            return CircuitBreakerState(**asdict(state))


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
        return self._parse_state(raw)

    def save(self, state: CircuitBreakerState) -> None:
        self._redis.set(self._key, json.dumps(asdict(state)))

    def mutate(self, mutator: Callable[[CircuitBreakerState], None]) -> CircuitBreakerState:
        with self._redis.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(self._key)  # type: ignore[no-untyped-call]
                    raw = pipe.get(self._key)
                    state = self._parse_state(raw)
                    mutator(state)
                    pipe.multi()
                    pipe.set(self._key, json.dumps(asdict(state)))
                    pipe.execute()
                    return CircuitBreakerState(**asdict(state))
                except WatchError:
                    continue
                finally:
                    pipe.reset()

    @staticmethod
    def _parse_state(raw: object | None) -> CircuitBreakerState:
        if not raw:
            return CircuitBreakerState()
        parsed = json.loads(cast("str", raw))
        return CircuitBreakerState(**parsed)


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
        self._safe_mutate(lambda _state: None)

    @property
    def state(self) -> str:
        """Current circuit state with automatic OPEN -> HALF_OPEN transition."""
        current_state = CircuitState.CLOSED.value

        def mutate(state: CircuitBreakerState) -> None:
            nonlocal current_state
            self._transition_if_reset_timeout_elapsed(state)
            current_state = state.state

        self._safe_mutate(mutate)
        return current_state

    @property
    def is_open(self) -> bool:
        """Check whether the circuit is open and update fail-open counters."""
        should_fail_open = False

        def mutate(state: CircuitBreakerState) -> None:
            nonlocal should_fail_open
            self._transition_if_reset_timeout_elapsed(state)

            if state.state == CircuitState.OPEN.value:
                state.total_fail_opens += 1
                should_fail_open = True
                return

            if state.state == CircuitState.HALF_OPEN.value:
                if state.half_open_probe_count < self._half_open_max_probes:
                    state.half_open_probe_count += 1
                    should_fail_open = False
                    return
                state.total_fail_opens += 1
                should_fail_open = True
                return

            should_fail_open = False

        self._safe_mutate(mutate)
        return should_fail_open

    def record_success(self) -> None:
        """Record a successful enrichment operation."""
        did_close = False

        def mutate(state: CircuitBreakerState) -> None:
            nonlocal did_close
            self._transition_if_reset_timeout_elapsed(state)

            if state.state == CircuitState.HALF_OPEN.value:
                state.half_open_success_count += 1
                if state.half_open_success_count >= self._half_open_max_probes:
                    state.state = CircuitState.CLOSED.value
                    state.failure_count = 0
                    state.half_open_success_count = 0
                    state.half_open_probe_count = 0
                    did_close = True
            else:
                state.failure_count = 0
                state.state = CircuitState.CLOSED.value

        self._safe_mutate(mutate)
        if did_close:
            logger.info("circuit_breaker.closed", reason="probes_succeeded")

    def record_failure(self) -> None:
        """Record a failed enrichment operation."""
        reopened = False
        opened = False
        failure_count = 0

        def mutate(state: CircuitBreakerState) -> None:
            nonlocal reopened, opened, failure_count
            self._transition_if_reset_timeout_elapsed(state)

            if state.state == CircuitState.HALF_OPEN.value:
                state.state = CircuitState.OPEN.value
                state.half_open_success_count = 0
                state.half_open_probe_count = 0
                state.failure_count = self._failure_threshold
                state.last_failure_time_epoch_s = time.time()
                state.total_opens += 1
                reopened = True
                return

            state.failure_count += 1
            state.last_failure_time_epoch_s = time.time()
            failure_count = state.failure_count

            if state.failure_count >= self._failure_threshold:
                state.state = CircuitState.OPEN.value
                state.half_open_success_count = 0
                state.half_open_probe_count = 0
                state.total_opens += 1
                opened = True

        self._safe_mutate(mutate)
        if reopened:
            logger.warning("circuit_breaker.reopened", reason="probe_failed")
        elif opened:
            logger.warning(
                "circuit_breaker.opened",
                failures=failure_count,
                threshold=self._failure_threshold,
            )

    def force_open(self) -> None:
        """Force the circuit open (e.g., during maintenance)."""
        def mutate(state: CircuitBreakerState) -> None:
            state.state = CircuitState.OPEN.value
            state.last_failure_time_epoch_s = time.time()
            state.half_open_success_count = 0
            state.half_open_probe_count = 0
            state.total_opens += 1

        self._safe_mutate(mutate)
        logger.info("circuit_breaker.force_opened")

    def force_close(self) -> None:
        """Force the circuit closed (e.g., after manual recovery)."""
        def mutate(state: CircuitBreakerState) -> None:
            state.state = CircuitState.CLOSED.value
            state.failure_count = 0
            state.half_open_success_count = 0
            state.half_open_probe_count = 0

        self._safe_mutate(mutate)
        logger.info("circuit_breaker.force_closed")

    def get_metrics(self) -> dict[str, int | str]:
        """Return circuit breaker metrics for monitoring."""
        state = self._safe_mutate(self._transition_if_reset_timeout_elapsed)
        return {
            "state": state.state,
            "failure_count": state.failure_count,
            "failure_threshold": self._failure_threshold,
            "half_open_probe_count": state.half_open_probe_count,
            "total_opens": state.total_opens,
            "total_fail_opens": state.total_fail_opens,
            "backend": type(self._state_store).__name__,
        }

    def _transition_if_reset_timeout_elapsed(self, state: CircuitBreakerState) -> None:
        if state.state != CircuitState.OPEN.value:
            return
        elapsed = time.time() - state.last_failure_time_epoch_s
        if elapsed >= self._reset_timeout_s:
            state.state = CircuitState.HALF_OPEN.value
            state.half_open_success_count = 0
            state.half_open_probe_count = 0

    def _safe_mutate(
        self,
        mutator: Callable[[CircuitBreakerState], None],
    ) -> CircuitBreakerState:
        try:
            return self._state_store.mutate(mutator)
        except RedisError as exc:
            logger.warning("circuit_breaker.state_mutation_failed", error=str(exc))
            fallback_state = CircuitBreakerState()
            mutator(fallback_state)
            return fallback_state
