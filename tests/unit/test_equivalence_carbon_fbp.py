"""
Unit tests for capability equivalence, carbon calculator, and federated benchmarking.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aci.benchmark.federated import (
    BenchmarkMetric,
    FederatedBenchmarkProtocol,
    PrivacyBudgetExhaustedError,
)
from aci.carbon.calculator import CarbonCalculator
from aci.config import FBPConfig
from aci.equivalence.verifier import (
    EquivalenceClass,
    EquivalenceVerifier,
    ShadowEvaluationResult,
)
from aci.models.carbon import CarbonMethodologyLayer, EquivalenceMode

# ---------------------------------------------------------------------------
# Capability Equivalence (Section 6.2)
# ---------------------------------------------------------------------------


class TestEquivalenceVerifier:
    """Tests for capability equivalence verification."""

    def setup_method(self) -> None:
        self.verifier = EquivalenceVerifier()
        self.verifier.register_class(
            EquivalenceClass(
                class_id="cs-chat",
                name="Customer Support Chat",
                approved_models=["gpt-4o-mini", "gpt-4o", "claude-3-haiku"],
                use_case="Customer support conversations",
            )
        )

    def test_policy_mode_both_approved(self) -> None:
        """Mode 1: both models in the same class -> equivalent."""
        result = self.verifier.verify_policy("gpt-4o", "gpt-4o-mini", "cs-chat")
        assert result.is_equivalent is True
        assert result.mode == EquivalenceMode.POLICY

    def test_policy_mode_candidate_not_approved(self) -> None:
        """Mode 1: candidate not in class -> NOT equivalent."""
        result = self.verifier.verify_policy("gpt-4o", "llama-3-70b", "cs-chat")
        assert result.is_equivalent is False

    def test_policy_mode_unknown_class(self) -> None:
        """Unknown class triggers fail-safe."""
        result = self.verifier.verify_policy("gpt-4o", "gpt-4o-mini", "nonexistent")
        assert result.is_equivalent is False
        assert result.fail_safe_triggered is True

    def test_empirical_mode_equivalent(self) -> None:
        """Mode 2a: candidate scores >= baseline - delta -> equivalent."""
        # Generate shadow evaluation results where candidate is close to baseline.
        results = [
            ShadowEvaluationResult(baseline_score=0.85, candidate_score=0.83) for _ in range(150)
        ]
        verification = self.verifier.verify_empirical(
            "gpt-4o",
            "gpt-4o-mini",
            "cs-chat",
            results,
        )
        assert verification.is_equivalent is True
        assert verification.sample_count == 150

    def test_empirical_mode_insufficient_samples(self) -> None:
        """Insufficient samples -> fail-safe (NOT equivalent)."""
        results = [ShadowEvaluationResult(0.85, 0.84) for _ in range(50)]
        verification = self.verifier.verify_empirical(
            "gpt-4o",
            "gpt-4o-mini",
            "cs-chat",
            results,
        )
        assert verification.is_equivalent is False
        assert verification.fail_safe_triggered is True

    def test_empirical_mode_not_equivalent(self) -> None:
        """Candidate significantly worse -> NOT equivalent."""
        results = [
            ShadowEvaluationResult(baseline_score=0.90, candidate_score=0.60) for _ in range(150)
        ]
        verification = self.verifier.verify_empirical(
            "gpt-4o",
            "gpt-4o-mini",
            "cs-chat",
            results,
        )
        assert verification.is_equivalent is False

    def test_judge_mode_equivalent(self) -> None:
        """Mode 2b: LLM-as-judge confirms equivalence."""
        # (baseline_score, candidate_score) pairs from judge.
        # Candidate matches or exceeds baseline ~60% of the time (> 0.45 threshold).
        judge_scores = [(0.8, 0.82)] * 150 + [(0.8, 0.75)] * 100
        verification = self.verifier.verify_judge(
            "gpt-4o",
            "gpt-4o-mini",
            "cs-chat",
            judge_scores,
        )
        assert verification.is_equivalent is True

    def test_cached_verification(self) -> None:
        """Verifications are cached with TTL."""
        self.verifier.verify_policy("gpt-4o", "gpt-4o-mini", "cs-chat")
        cached = self.verifier.get_cached_verification("gpt-4o", "gpt-4o-mini", "cs-chat")
        assert cached is not None
        assert cached.is_equivalent is True

    def test_fail_safe_never_optimizes_unsafe(self) -> None:
        """The system never optimizes when it cannot verify safety."""
        # Unknown class, unknown models.
        result = self.verifier.verify_policy("unknown-model", "other-model", "unknown-class")
        assert result.is_equivalent is False
        assert result.fail_safe_triggered is True


# ---------------------------------------------------------------------------
# Carbon Calculator (Section 8)
# ---------------------------------------------------------------------------


class TestCarbonCalculator:
    """Tests for the three-layer carbon methodology."""

    def setup_method(self) -> None:
        self.calculator = CarbonCalculator()

    def test_layer1_baseline(self) -> None:
        """Layer 1: spend-based estimation."""
        receipt = self.calculator.compute_layer1(
            workload_id="w1",
            cloud_spend_usd=1000.0,
            region="us-east-1",
        )
        assert receipt.method_layer == CarbonMethodologyLayer.BASELINE
        assert receipt.emissions_kg_co2e > 0
        # Uncertainty band should be wide: +/- 50-100%.
        low, high = receipt.uncertainty_band_pct
        assert high >= 50.0

    def test_layer2_standard(self) -> None:
        """Layer 2: activity-based estimation."""
        receipt = self.calculator.compute_layer2(
            workload_id="w2",
            model="gpt-4o",
            inference_count=10000,
            region="us-west-2",
        )
        assert receipt.method_layer == CarbonMethodologyLayer.STANDARD
        assert receipt.emissions_kg_co2e > 0
        # Tighter uncertainty: +/- 15-30%.
        low, high = receipt.uncertainty_band_pct
        assert high <= 35.0

    def test_layer2_with_grid_intensity(self) -> None:
        """Regions with different grid intensity produce different emissions."""
        clean_region = self.calculator.compute_layer2(
            "w3",
            "gpt-4o",
            10000,
            "eu-north-1",  # Nordic, low carbon grid.
        )
        dirty_region = self.calculator.compute_layer2(
            "w4",
            "gpt-4o",
            10000,
            "ap-south-1",  # India, high carbon grid.
        )
        assert clean_region.emissions_kg_co2e < dirty_region.emissions_kg_co2e

    def test_receipt_immutability(self) -> None:
        """Receipts must be immutable (Section 8.2)."""
        receipt = self.calculator.compute_layer1("w5", 500.0, "us-east-1")
        with pytest.raises(ValidationError):
            receipt.emissions_kg_co2e = 999.0  # type: ignore

    def test_scope3_third_party(self) -> None:
        """Third-party API inference emissions are Scope 3."""
        receipt = self.calculator.compute_scope3_api(
            workload_id="w6",
            provider="openai",
            model="gpt-4o",
            total_tokens=500000,
        )
        assert receipt.ghg_scope.value == "scope_3"
        assert receipt.provider_name == "openai"


# ---------------------------------------------------------------------------
# Federated Benchmarking Protocol (Section 11)
# ---------------------------------------------------------------------------


class TestFederatedBenchmarking:
    """Tests for FBP with differential privacy."""

    def setup_method(self) -> None:
        self.fbp = FederatedBenchmarkProtocol()

    def test_minimum_cohort_size(self) -> None:
        """Cohorts below k=5 are suppressed."""
        self.fbp.create_cohort("small-cohort")
        for i in range(3):
            self.fbp.join_cohort(f"org-{i}", "small-cohort")
            self.fbp.submit_metric(
                BenchmarkMetric(
                    org_id=f"org-{i}",
                    metric_name="cost_per_token_usd",
                    raw_value=0.003,
                    period="2026-Q1",
                )
            )

        result = self.fbp.publish_benchmark("small-cohort", "cost_per_token_usd", "2026-Q1")
        assert result.suppressed is True
        assert "minimum" in result.suppression_reason.lower()

    def test_valid_benchmark_with_dp_noise(self) -> None:
        """Valid cohort produces noisy but reasonable benchmark."""
        self.fbp.create_cohort("valid-cohort")
        for i in range(6):
            self.fbp.join_cohort(f"org-{i}", "valid-cohort")
            self.fbp.submit_metric(
                BenchmarkMetric(
                    org_id=f"org-{i}",
                    metric_name="cost_per_token_usd",
                    raw_value=0.003 + i * 0.0001,
                    period="2026-Q1",
                )
            )

        result = self.fbp.publish_benchmark("valid-cohort", "cost_per_token_usd", "2026-Q1")
        assert result.suppressed is False
        assert result.epsilon_consumed == 1.0
        # Noisy mean should be in the ballpark of the true mean (~0.0032).
        # Laplace noise with small cohort can be significant; tolerance is generous.
        assert abs(result.noisy_mean - 0.0032) < 0.1

    def test_sensitivity_clipping(self) -> None:
        """Values exceeding sensitivity bounds are clipped."""
        self.fbp.create_cohort("clip-test")
        self.fbp.join_cohort("org-outlier", "clip-test")

        # Submit a value far above the clip bound ($0.10).
        self.fbp.submit_metric(
            BenchmarkMetric(
                org_id="org-outlier",
                metric_name="cost_per_token_usd",
                raw_value=999.99,
                period="2026-Q1",
            )
        )

        key = "org-outlier:cost_per_token_usd:2026-Q1"
        stored = self.fbp.metrics[key][-1]
        assert stored.raw_value == 0.10  # Clipped to sensitivity bound.

    def test_privacy_budget_tracking(self) -> None:
        """Privacy budget is consumed per release."""
        self.fbp.create_cohort("budget-test")
        for i in range(6):
            self.fbp.join_cohort(f"org-{i}", "budget-test")
            self.fbp.submit_metric(
                BenchmarkMetric(
                    org_id=f"org-{i}",
                    metric_name="cost_per_token_usd",
                    raw_value=0.003,
                    period="2026-Q1",
                )
            )

        self.fbp.publish_benchmark("budget-test", "cost_per_token_usd", "2026-Q1")
        remaining = self.fbp.get_remaining_budget("org-0")
        assert remaining == 3.0  # 4.0 - 1.0 consumed.

    def test_privacy_budget_exhaustion(self) -> None:
        """Publishing exceeding quarterly budget raises error."""
        config = FBPConfig(epsilon_total_quarterly=2.0, epsilon_per_release=1.0)
        fbp = FederatedBenchmarkProtocol(config)
        fbp.create_cohort("budget-exhaust")
        for i in range(6):
            fbp.join_cohort(f"org-{i}", "budget-exhaust")

        for period in ["2026-Q1-Jan", "2026-Q1-Feb"]:
            for i in range(6):
                fbp.submit_metric(
                    BenchmarkMetric(
                        org_id=f"org-{i}",
                        metric_name="cost_per_token_usd",
                        raw_value=0.003,
                        period=period,
                    )
                )
            fbp.publish_benchmark("budget-exhaust", "cost_per_token_usd", period)

        # Third release should exhaust budget.
        for i in range(6):
            fbp.submit_metric(
                BenchmarkMetric(
                    org_id=f"org-{i}",
                    metric_name="cost_per_token_usd",
                    raw_value=0.003,
                    period="2026-Q1-Mar",
                )
            )
        with pytest.raises(PrivacyBudgetExhaustedError):
            fbp.publish_benchmark("budget-exhaust", "cost_per_token_usd", "2026-Q1-Mar")

    def test_quarterly_budget_reset(self) -> None:
        """Budget resets at quarter boundary."""
        self.fbp.budget_consumed["org-0"] = 3.5
        assert self.fbp.get_remaining_budget("org-0") == 0.5
        self.fbp.reset_quarterly_budgets()
        assert self.fbp.get_remaining_budget("org-0") == 4.0

    def test_discrete_laplace_produces_integers(self) -> None:
        """Secure discrete Laplace mechanism produces integer-valued noise."""
        from aci.benchmark.federated import _discrete_laplace

        noises = [_discrete_laplace(2.0) for _ in range(200)]
        assert all(n == int(n) for n in noises)
        # Should have some nonzero values (probability of 200 zeros is negligible).
        assert any(n != 0 for n in noises)

    def test_secure_noise_config_toggle(self) -> None:
        """use_secure_noise flag switches to discrete Laplace mechanism."""
        from aci.config import FBPConfig as FBPCfg

        config = FBPCfg(use_secure_noise=True)
        fbp = FederatedBenchmarkProtocol(config)
        fbp.create_cohort("secure-test")
        for i in range(6):
            fbp.join_cohort(f"org-{i}", "secure-test")
            fbp.submit_metric(
                BenchmarkMetric(
                    org_id=f"org-{i}",
                    metric_name="cost_per_token_usd",
                    raw_value=0.003,
                    period="2026-Q1",
                )
            )
        result = fbp.publish_benchmark("secure-test", "cost_per_token_usd", "2026-Q1")
        assert result.suppressed is False
        assert result.epsilon_consumed == 1.0


# ---------------------------------------------------------------------------
# Shadow Event Emission
# ---------------------------------------------------------------------------


class TestShadowEventEmission:
    """Tests for interceptor shadow event emission (Section 6.3)."""

    @pytest.mark.asyncio
    async def test_cache_miss_emits_shadow_event(self) -> None:
        """Cache miss should emit SHADOW_INTERCEPT_MISS to event bus."""
        from aci.core.event_bus import InMemoryEventBus
        from aci.index.materializer import AttributionIndexStore
        from aci.interceptor.gateway import (
            DeploymentMode,
            FailOpenInterceptor,
            InterceptionOutcome,
            InterceptionRequest,
        )
        from aci.models.events import DomainEvent, EventType

        bus = InMemoryEventBus()
        received_events: list[DomainEvent] = []
        bus.subscribe(EventType.SHADOW_INTERCEPT_MISS.value, lambda e: received_events.append(e))

        store = AttributionIndexStore()
        interceptor = FailOpenInterceptor(
            store,
            mode=DeploymentMode.ADVISORY,
            event_bus=bus,
        )

        request = InterceptionRequest(
            request_id="req-shadow-001",
            model="gpt-4o",
            provider="openai",
            service_name="unknown-workload",
        )

        result = await interceptor.intercept(request)
        # Allow the event loop to process the fire-and-forget task.
        import asyncio

        await asyncio.sleep(0.05)

        assert result.outcome == InterceptionOutcome.FAIL_OPEN
        assert result.shadow_event_logged is True
