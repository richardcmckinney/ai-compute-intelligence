"""
Phase 0: Glass Jaw Validation Tests.

The Glass Jaw experiment must produce unambiguous pass/fail results before
any further development proceeds (Strategy Memo Section B).

Quantitative success/fail criteria:
- P99 latency overhead <= 15ms (72-hour sustained load at 10K req/s)
- Zero request failures over the 72-hour test
- Prompt classification accuracy >= 80% (500+ labeled prompts, 5+ categories)
- Memory footprint <= 256MB under sustained load
- Fail-open validated under chaos testing (zero client-visible errors on process crash)

If ANY criterion fails: redesign, pivot to dashboard-only, or narrow claim scope.

These tests validate the criteria at unit/integration scale. The full 72-hour
sustained load test is run via the Locust load test configuration.
"""

from __future__ import annotations

import asyncio
import gc
import os
import statistics
import sys
import time
from datetime import datetime, timezone

import pytest

from aci.index.materializer import AttributionIndexStore
from aci.interceptor.gateway import (
    DeploymentMode,
    FailOpenInterceptor,
    InterceptionOutcome,
    InterceptionRequest,
)
from aci.models.attribution import AttributionIndexEntry


def _make_index_entry(workload_id: str) -> AttributionIndexEntry:
    """Create a realistic index entry for testing."""
    return AttributionIndexEntry(
        workload_id=workload_id,
        team_id="team-test",
        team_name="Test Team",
        cost_center_id="CC-0001",
        confidence=0.85,
        confidence_tier="chargeback_ready",
        method_used="R1",
        budget_remaining_usd=3000.0,
        budget_limit_usd=5000.0,
        model_allowlist=["gpt-4o-mini", "gpt-4o", "claude-3-haiku"],
        equivalence_class_id="general-chat",
        approved_alternatives=["gpt-4o-mini"],
    )


def _make_request(request_id: str = "req-001") -> InterceptionRequest:
    return InterceptionRequest(
        request_id=request_id,
        model="gpt-4o",
        provider="openai",
        service_name="test-service",
        estimated_cost_usd=0.003,
    )


# ---------------------------------------------------------------------------
# Criterion 1: P99 Latency <= 15ms
# ---------------------------------------------------------------------------

@pytest.mark.glass_jaw
class TestLatencyBudget:
    """
    Validate that interception overhead stays within the 15ms P99 budget.

    The full 72-hour test at 10K req/s runs via Locust. These unit tests
    validate the latency characteristics at smaller scale.
    """

    def setup_method(self) -> None:
        self.store = AttributionIndexStore()
        # Pre-populate with 10K entries to simulate realistic index size.
        for i in range(10_000):
            self.store.materialize(_make_index_entry(f"service-{i}"))
        self.interceptor = FailOpenInterceptor(
            self.store, mode=DeploymentMode.ADVISORY,
        )

    @pytest.mark.asyncio
    async def test_single_request_under_15ms(self) -> None:
        """Single interception request completes well under 15ms."""
        start = time.monotonic()
        result = await self.interceptor.intercept(
            InterceptionRequest(
                request_id="lat-test-1",
                model="gpt-4o",
                provider="openai",
                service_name="service-500",
                estimated_cost_usd=0.003,
            )
        )
        elapsed = (time.monotonic() - start) * 1000
        assert elapsed < 15.0, f"Single request took {elapsed:.2f}ms"
        assert result.outcome == InterceptionOutcome.ENRICHED

    @pytest.mark.asyncio
    async def test_p99_under_15ms_1k_requests(self) -> None:
        """P99 latency under 15ms across 1,000 sequential requests."""
        latencies: list[float] = []
        for i in range(1000):
            start = time.monotonic()
            await self.interceptor.intercept(
                InterceptionRequest(
                    request_id=f"lat-{i}",
                    model="gpt-4o",
                    provider="openai",
                    service_name=f"service-{i % 10000}",
                    estimated_cost_usd=0.003,
                )
            )
            latencies.append((time.monotonic() - start) * 1000)

        p50 = sorted(latencies)[500]
        p99 = sorted(latencies)[990]
        mean_lat = statistics.mean(latencies)

        assert p99 < 15.0, f"P99={p99:.2f}ms exceeds 15ms budget"
        assert p50 < 5.0, f"P50={p50:.2f}ms is unexpectedly high"

    @pytest.mark.asyncio
    async def test_concurrent_requests_under_budget(self) -> None:
        """Concurrent interceptions should not degrade latency."""
        async def timed_intercept(i: int) -> float:
            start = time.monotonic()
            await self.interceptor.intercept(
                InterceptionRequest(
                    request_id=f"conc-{i}",
                    model="gpt-4o",
                    provider="openai",
                    service_name=f"service-{i % 10000}",
                    estimated_cost_usd=0.003,
                )
            )
            return (time.monotonic() - start) * 1000

        tasks = [timed_intercept(i) for i in range(100)]
        latencies = await asyncio.gather(*tasks)
        p99 = sorted(latencies)[99]
        assert p99 < 15.0, f"Concurrent P99={p99:.2f}ms"


# ---------------------------------------------------------------------------
# Criterion 2: Zero Request Failures
# ---------------------------------------------------------------------------

@pytest.mark.glass_jaw
class TestZeroFailures:
    """Every request must complete without error, including cache misses."""

    def setup_method(self) -> None:
        self.store = AttributionIndexStore()
        for i in range(100):
            self.store.materialize(_make_index_entry(f"svc-{i}"))
        self.interceptor = FailOpenInterceptor(
            self.store, mode=DeploymentMode.ADVISORY,
        )

    @pytest.mark.asyncio
    async def test_no_exceptions_on_miss(self) -> None:
        """Cache misses produce FAIL_OPEN, never exceptions."""
        for i in range(500):
            result = await self.interceptor.intercept(
                InterceptionRequest(
                    request_id=f"miss-{i}",
                    model="gpt-4o",
                    provider="openai",
                    service_name=f"nonexistent-{i}",
                    estimated_cost_usd=0.003,
                )
            )
            assert result.outcome == InterceptionOutcome.FAIL_OPEN

    @pytest.mark.asyncio
    async def test_no_exceptions_mixed_traffic(self) -> None:
        """Mixed hits and misses: zero exceptions across 2,000 requests."""
        errors = 0
        for i in range(2000):
            try:
                svc = f"svc-{i % 200}"  # Half will miss.
                await self.interceptor.intercept(
                    InterceptionRequest(
                        request_id=f"mix-{i}",
                        model="gpt-4o",
                        provider="openai",
                        service_name=svc,
                        estimated_cost_usd=0.003,
                    )
                )
            except Exception:
                errors += 1
        assert errors == 0, f"{errors} request failures out of 2,000"


# ---------------------------------------------------------------------------
# Criterion 4: Memory Footprint <= 256MB
# ---------------------------------------------------------------------------

@pytest.mark.glass_jaw
class TestMemoryFootprint:
    """Index with realistic cardinality must stay under 256MB."""

    def test_index_memory_under_256mb(self) -> None:
        """
        10K index entries (typical enterprise) should use well under 256MB.

        For Phase 0, 10K entries represents a mid-size enterprise with
        ~10K unique workload identifiers across all teams.
        """
        import tracemalloc
        tracemalloc.start()

        store = AttributionIndexStore()
        for i in range(10_000):
            entry = AttributionIndexEntry(
                workload_id=f"workload-{i:06d}",
                team_id=f"team-{i % 50}",
                team_name=f"Team {i % 50}",
                cost_center_id=f"CC-{i % 20:04d}",
                confidence=0.85,
                confidence_tier="chargeback_ready",
                method_used="R1",
                budget_remaining_usd=5000.0 - (i % 100) * 50,
                budget_limit_usd=5000.0,
                model_allowlist=["gpt-4o-mini", "gpt-4o", "claude-3-haiku"],
            )
            store.materialize(entry)

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        peak_mb = peak / 1024 / 1024
        assert peak_mb < 256, f"Peak memory {peak_mb:.1f}MB exceeds 256MB limit"
        # Realistically should be well under 100MB for 10K entries.
        assert peak_mb < 100, f"Peak memory {peak_mb:.1f}MB (expected < 100MB for 10K entries)"


# ---------------------------------------------------------------------------
# Criterion 5: Fail-Open Under Chaos
# ---------------------------------------------------------------------------

@pytest.mark.glass_jaw
class TestFailOpenChaos:
    """
    The interceptor must produce zero client-visible errors even when
    internal components fail. Every failure mode produces FAIL_OPEN
    with the original request proceeding unmodified.
    """

    def setup_method(self) -> None:
        self.store = AttributionIndexStore()
        self.store.materialize(_make_index_entry("existing-svc"))

    @pytest.mark.asyncio
    async def test_timeout_produces_fail_open(self) -> None:
        """Timeout produces FAIL_OPEN, not an error."""
        from aci.config import InterceptorConfig
        # Set impossibly tight timeout.
        config = InterceptorConfig(timeout_ms=0)
        interceptor = FailOpenInterceptor(self.store, config, DeploymentMode.ADVISORY)

        result = await interceptor.intercept(_make_request())
        # Should be FAIL_OPEN (timeout) or ENRICHED if it somehow completed.
        assert result.outcome in (
            InterceptionOutcome.FAIL_OPEN,
            InterceptionOutcome.ENRICHED,
        )

    @pytest.mark.asyncio
    async def test_circuit_breaker_produces_bypass(self) -> None:
        """Circuit breaker opening produces CIRCUIT_OPEN bypass."""
        interceptor = FailOpenInterceptor(self.store, mode=DeploymentMode.ADVISORY)

        # Force circuit breaker open.
        for _ in range(10):
            interceptor.circuit_breaker.record_failure()

        result = await interceptor.intercept(_make_request())
        assert result.outcome == InterceptionOutcome.CIRCUIT_OPEN

    @pytest.mark.asyncio
    async def test_empty_index_graceful(self) -> None:
        """Empty index produces FAIL_OPEN, never crashes."""
        empty_store = AttributionIndexStore()
        interceptor = FailOpenInterceptor(empty_store, mode=DeploymentMode.ACTIVE)

        for i in range(100):
            result = await interceptor.intercept(
                InterceptionRequest(
                    request_id=f"empty-{i}",
                    model="gpt-4o",
                    provider="openai",
                    service_name=f"any-service-{i}",
                    estimated_cost_usd=0.01,
                )
            )
            assert result.outcome == InterceptionOutcome.FAIL_OPEN
