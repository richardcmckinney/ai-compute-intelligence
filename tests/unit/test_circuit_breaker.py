from __future__ import annotations

import time

from aci.interceptor.circuit_breaker import CircuitBreaker, CircuitState



def test_opens_after_threshold_failures() -> None:
    cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=1.0, half_open_max_probes=2)

    cb.record_failure()
    assert cb.state == CircuitState.CLOSED.value

    cb.record_failure()
    assert cb.state == CircuitState.OPEN.value
    assert cb.is_open is True



def test_half_open_limits_probe_requests() -> None:
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_s=0.01, half_open_max_probes=2)

    cb.record_failure()
    assert cb.state == CircuitState.OPEN.value

    time.sleep(0.02)

    # Two probes allowed.
    assert cb.is_open is False
    assert cb.is_open is False

    # Further requests are blocked until probes succeed or fail.
    assert cb.is_open is True



def test_successful_half_open_probes_close_circuit() -> None:
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_s=0.01, half_open_max_probes=2)

    cb.record_failure()
    time.sleep(0.02)

    assert cb.is_open is False
    cb.record_success()
    assert cb.state == CircuitState.HALF_OPEN.value

    assert cb.is_open is False
    cb.record_success()
    assert cb.state == CircuitState.CLOSED.value
    assert cb.is_open is False



def test_failed_half_open_probe_reopens_circuit() -> None:
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_s=0.01, half_open_max_probes=2)

    cb.record_failure()
    time.sleep(0.02)

    assert cb.is_open is False
    cb.record_failure()

    assert cb.state == CircuitState.OPEN.value
    assert cb.is_open is True
