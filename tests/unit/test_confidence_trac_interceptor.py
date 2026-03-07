"""
Unit tests for confidence calibration, TRAC calculator, and fail-open interceptor.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from aci.confidence.calibration import CalibrationEngine
from aci.config import InterceptorConfig
from aci.index.materializer import AttributionIndexStore
from aci.interceptor.gateway import (
    DeploymentMode,
    FailOpenInterceptor,
    InterceptionOutcome,
    InterceptionRequest,
)
from aci.models.attribution import AttributionIndexEntry
from aci.models.confidence import ConfidenceTier, GroundTruthLabel
from aci.models.events import DomainEvent, EventType
from aci.trac.calculator import TRACCalculator, TRACWorkloadInput

if TYPE_CHECKING:
    from aci.models.carbon import PolicyEvaluationResult

# ---------------------------------------------------------------------------
# Confidence Calibration
# ---------------------------------------------------------------------------


class TestCalibrationEngine:
    """Tests for the confidence calibration system (Section 5.2)."""

    def setup_method(self) -> None:
        self.engine = CalibrationEngine()

    def test_warm_start_curves_loaded(self) -> None:
        """All R1-R6 should have warm-start curves at initialization."""
        for method in ["R1", "R2", "R3", "R4", "R5", "R6"]:
            assert method in self.engine.curves
            assert self.engine.curves[method].is_warm_start is True

    def test_calibrate_score_with_warm_start(self) -> None:
        """Warm-start calibration should produce reasonable scores."""
        # R1 at 0.9 raw should calibrate close to 0.9 (R1 is most reliable).
        calibrated = self.engine.calibrate("R1", 0.9)
        assert 0.7 <= calibrated <= 0.95

        # R6 (weakest method) should produce lower calibrated scores.
        r6_cal = self.engine.calibrate("R6", 0.9)
        r1_cal = self.engine.calibrate("R1", 0.9)
        assert r6_cal <= r1_cal

    def test_cap_enforced(self) -> None:
        """No calibrated score should exceed 0.95 after isotonic fit."""
        # Add enough ground truth to trigger isotonic fit for R1.
        import random

        random.seed(42)
        for i in range(250):
            self.engine.add_ground_truth(
                GroundTruthLabel(
                    attribution_id=f"attr_{i}",
                    true_team_id="team-a",
                    true_cost_center_id="cc-1",
                    source="cicd_provenance",
                    predicted_team_id="team-a",
                    predicted_confidence=1.0,
                    method_used="R1",
                    was_correct=True,
                )
            )
        calibrated = self.engine.calibrate("R1", 1.0)
        assert calibrated <= 0.95

    def test_confidence_tier_classification(self) -> None:
        assert self.engine.get_confidence_tier(0.85) == ConfidenceTier.CHARGEBACK_READY
        assert self.engine.get_confidence_tier(0.65) == ConfidenceTier.PROVISIONAL
        assert self.engine.get_confidence_tier(0.30) == ConfidenceTier.ESTIMATED

    def test_insufficient_samples_keeps_warm_start(self) -> None:
        """Below 50 samples: warm-start curve is unchanged."""
        for i in range(30):
            self.engine.add_ground_truth(
                GroundTruthLabel(
                    attribution_id=f"attr_{i}",
                    true_team_id="team-a",
                    true_cost_center_id="cc-1",
                    source="cicd_provenance",
                    predicted_team_id="team-a",
                    predicted_confidence=0.8,
                    method_used="R2",
                    was_correct=True,
                )
            )
        # R2 should still be warm-start (no fit triggered below 50 samples).
        assert self.engine.curves["R2"].is_warm_start is True

    def test_sufficient_samples_fits_isotonic(self) -> None:
        """At 200+ samples, full isotonic regression kicks in."""
        import random

        random.seed(42)
        for i in range(250):
            conf = random.uniform(0.3, 0.95)
            correct = random.random() < conf
            self.engine.add_ground_truth(
                GroundTruthLabel(
                    attribution_id=f"attr_{i}",
                    true_team_id="team-a",
                    true_cost_center_id="cc-1",
                    source="finops_review",
                    predicted_team_id="team-a" if correct else "team-b",
                    predicted_confidence=conf,
                    method_used="R2",
                    was_correct=correct,
                )
            )
        assert self.engine.curves["R2"].sample_count == 250
        assert self.engine.curves["R2"].is_warm_start is False

    def test_temporal_decay(self) -> None:
        """Confidence decays for signals older than the recency window."""
        # Within window: no decay.
        assert self.engine.apply_temporal_decay(0.8, signal_age_days=30) == 0.8

        # Beyond window: decayed.
        decayed = self.engine.apply_temporal_decay(0.8, signal_age_days=150)
        assert decayed < 0.8
        assert decayed > 0.0

    def test_interpolate_handles_duplicate_raw_points(self) -> None:
        """Duplicate raw-score points should not trigger division by zero."""
        calibrated = self.engine._interpolate(
            [0.2, 0.2, 0.4],
            [0.1, 0.3, 0.5],
            0.2,
        )
        assert calibrated == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# TRAC Calculator
# ---------------------------------------------------------------------------


class TestTRACCalculator:
    """Tests for TRAC computation (Section 7)."""

    def setup_method(self) -> None:
        self.trac = TRACCalculator()

    def test_patent_example_deterministic(self) -> None:
        """
        Section 13.1: fraud-v2 workload.
        Billed $4.80, emissions 0.0004 kgCO2e, confidence 1.0.
        TRAC ~= $4.80 (negligible carbon and zero risk premium).
        """
        result = self.trac.compute(
            workload_id="fraud-v2",
            billed_cost_usd=4.80,
            emissions_kg_co2e=0.0004,
            attribution_confidence=1.0,
        )
        assert abs(result.trac_usd - 4.80) < 0.01
        assert result.confidence_risk_premium_usd == 0.0
        assert result.carbon_pct_of_trac < 1.0

    def test_patent_sensitivity_note(self) -> None:
        """
        Section 7 sensitivity note:
        $100/day workload, $50/tCO2e, 70% confidence.
        Risk premium: $100 * 0.3 * 0.15 = $4.50.
        """
        result = self.trac.compute(
            workload_id="typical-workload",
            billed_cost_usd=100.0,
            emissions_kg_co2e=1.0,
            attribution_confidence=0.70,
        )
        assert result.confidence_risk_premium_usd == pytest.approx(4.50, abs=0.01)
        assert result.carbon_liability_usd == pytest.approx(0.05, abs=0.01)
        assert result.risk_pct_of_trac > result.carbon_pct_of_trac

    def test_temporal_decay_increases_risk(self) -> None:
        """Stale signals should increase the risk premium."""
        fresh = self.trac.compute("w1", 100.0, 1.0, 0.8, signal_age_days=0)
        stale = self.trac.compute("w1", 100.0, 1.0, 0.8, signal_age_days=180)
        assert stale.confidence_risk_premium_usd > fresh.confidence_risk_premium_usd

    def test_zero_cost(self) -> None:
        """Edge case: zero billed cost."""
        result = self.trac.compute("w0", 0.0, 0.0, 0.5)
        assert result.trac_usd == 0.0

    def test_batch_computation(self) -> None:
        """Batch TRAC computation."""
        workloads: list[TRACWorkloadInput] = [
            {"workload_id": "a", "billed_cost_usd": 50.0, "attribution_confidence": 0.9},
            {"workload_id": "b", "billed_cost_usd": 200.0, "attribution_confidence": 0.6},
        ]
        results = self.trac.compute_batch(workloads)
        assert len(results) == 2
        assert results[1].trac_usd > results[0].trac_usd


# ---------------------------------------------------------------------------
# Fail-Open Interceptor
# ---------------------------------------------------------------------------


class TestFailOpenInterceptor:
    """Tests for the fail-open decision-time interceptor (Section 6)."""

    def setup_method(self) -> None:
        self.store = AttributionIndexStore()
        self.interceptor = FailOpenInterceptor(
            self.store,
            mode=DeploymentMode.ADVISORY,
        )

    def _make_request(
        self, service_name: str = "test-svc", model: str = "gpt-4o"
    ) -> InterceptionRequest:
        return InterceptionRequest(
            request_id="req-001",
            model=model,
            provider="openai",
            service_name=service_name,
            estimated_cost_usd=0.002,
        )

    def _make_index_entry(self, workload_id: str = "test-svc") -> AttributionIndexEntry:
        return AttributionIndexEntry(
            workload_id=workload_id,
            team_id="team-cs",
            team_name="CS-Platform",
            cost_center_id="CC-1200",
            confidence=0.85,
            confidence_tier="chargeback_ready",
            method_used="R1",
            budget_remaining_usd=1800.0,
            budget_limit_usd=5000.0,
        )

    @pytest.mark.asyncio
    async def test_cache_miss_fail_open(self) -> None:
        """Cache miss: request proceeds unmodified (fail-open)."""
        result = await self.interceptor.intercept(self._make_request())
        assert result.outcome == InterceptionOutcome.FAIL_OPEN
        assert result.shadow_event_logged is False

    @pytest.mark.asyncio
    async def test_advisory_enrichment(self) -> None:
        """Advisory mode: enriches headers, does not redirect."""
        self.store.materialize(self._make_index_entry())
        result = await self.interceptor.intercept(self._make_request())
        assert result.outcome == InterceptionOutcome.ENRICHED
        assert "X-ACI-Attribution-Owner-Id" in result.enrichment_headers
        assert result.enrichment_headers["X-ACI-Attribution-Owner-Id"] == "team-cs"
        assert result.enrichment_headers["X-ACI-Team-Name"] == "CS-Platform"
        assert result.redirect_model is None

    @pytest.mark.asyncio
    async def test_advisory_enrichment_includes_price_snapshot_header(self) -> None:
        self.store.materialize(self._make_index_entry())
        request = self._make_request()
        request.metadata["price_snapshot_id"] = "pricing-demo-123"
        result = await self.interceptor.intercept(request)
        assert result.outcome == InterceptionOutcome.ENRICHED
        assert result.enrichment_headers["X-ACI-Price-Snapshot-Id"] == "pricing-demo-123"

    @pytest.mark.asyncio
    async def test_passive_mode_no_enrichment(self) -> None:
        """Passive mode: logs only, no headers."""
        self.store.materialize(self._make_index_entry())
        self.interceptor.mode = DeploymentMode.PASSIVE
        result = await self.interceptor.intercept(self._make_request())
        assert result.outcome == InterceptionOutcome.PASSTHROUGH
        assert result.enrichment_headers == {}

    @pytest.mark.asyncio
    async def test_budget_violation_soft_stop(self) -> None:
        """Budget > 90% utilization triggers soft stop."""
        entry = self._make_index_entry()
        entry = entry.model_copy(
            update={
                "budget_remaining_usd": 100.0,
                "budget_limit_usd": 5000.0,
            }
        )
        self.store.materialize(entry)
        result = await self.interceptor.intercept(self._make_request())
        assert result.outcome == InterceptionOutcome.SOFT_STOPPED
        violations = [p for p in result.policy_results if p.violated]
        assert len(violations) > 0

    @pytest.mark.asyncio
    async def test_latency_under_budget(self) -> None:
        """Interception should complete well under 50ms with in-memory store."""
        self.store.materialize(self._make_index_entry())
        result = await self.interceptor.intercept(self._make_request())
        assert result.elapsed_ms < 50.0

    @pytest.mark.asyncio
    async def test_patent_example_gateway_decision(self) -> None:
        """
        Section 13.2: customer-support-bot gateway decision.
        Model gpt-4o-mini, CS-Platform team, $5K budget, $3.2K spent.
        Expected: enriched headers, no redirect, budget remaining 36%.
        """
        entry = AttributionIndexEntry(
            workload_id="customer-support-bot",
            team_id="team-cs-platform",
            team_name="CS-Platform",
            cost_center_id="CC-1200",
            confidence=0.92,
            confidence_tier="chargeback_ready",
            method_used="R1",
            budget_remaining_usd=1800.0,
            budget_limit_usd=5000.0,
            model_allowlist=["gpt-4o-mini", "gpt-4o", "gemini-1.5-flash"],
        )
        self.store.materialize(entry)

        request = InterceptionRequest(
            request_id="req-cs-001",
            model="gpt-4o-mini",
            provider="openai",
            service_name="customer-support-bot",
            estimated_cost_usd=0.0012,
        )

        result = await self.interceptor.intercept(request)
        assert result.outcome == InterceptionOutcome.ENRICHED
        assert result.redirect_model is None
        assert "X-ACI-Budget-Remaining-Pct" in result.enrichment_headers
        assert result.enrichment_headers["X-ACI-Budget-Remaining-Pct"] == "36"

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_failures(self) -> None:
        """Circuit breaker opens after consecutive failures."""
        self.store.materialize(self._make_index_entry())
        for _ in range(5):
            self.interceptor.circuit_breaker.record_failure()

        assert self.interceptor.circuit_breaker.state == "open"

        result = await self.interceptor.intercept(self._make_request())
        assert result.outcome == InterceptionOutcome.CIRCUIT_OPEN

    @pytest.mark.asyncio
    async def test_active_gate_requires_higher_confidence_than_shape(self) -> None:
        entry = AttributionIndexEntry(
            workload_id="gate-confidence-svc",
            team_id="team-gov",
            team_name="Governance",
            cost_center_id="CC-2200",
            confidence=0.92,
            confidence_tier="chargeback_ready",
            method_used="R2",
            token_budget_input=100,
        )
        self.store.materialize(entry)

        active = FailOpenInterceptor(
            self.store,
            config=InterceptorConfig(
                active_lite_min_confidence=0.90,
                active_lite_gate_min_confidence=0.95,
            ),
            mode=DeploymentMode.ACTIVE,
        )
        request = InterceptionRequest(
            request_id="req-gate-confidence",
            model="gpt-4o",
            service_name="gate-confidence-svc",
            input_tokens=250,
            metadata={"environment": "staging"},
        )

        result = await active.intercept(request)
        assert result.outcome == InterceptionOutcome.SOFT_STOPPED

    @pytest.mark.asyncio
    async def test_cost_ceiling_violation_surfaces_policy_result(self) -> None:
        self.store.materialize(
            AttributionIndexEntry(
                workload_id="svc-cost-cap",
                team_id="team-finops",
                team_name="FinOps",
                cost_center_id="CC-4400",
                confidence=0.94,
                confidence_tier="chargeback_ready",
                method_used="R2",
                cost_ceiling_per_request_usd=0.001,
            )
        )
        request = InterceptionRequest(
            request_id="req-cost-cap",
            model="gpt-4o",
            service_name="svc-cost-cap",
            estimated_cost_usd=0.01,
        )

        result = await self.interceptor.intercept(request)
        assert result.outcome == InterceptionOutcome.SOFT_STOPPED
        assert any(v.policy_id == "cost_ceiling" for v in result.policy_results)

    @pytest.mark.asyncio
    async def test_policy_timeout_triggers_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store.materialize(self._make_index_entry("svc-policy-timeout"))
        interceptor = FailOpenInterceptor(
            self.store,
            config=InterceptorConfig(policy_timeout_ms=1),
            mode=DeploymentMode.ADVISORY,
        )

        def delayed_eval(
            request: InterceptionRequest,
            attribution: AttributionIndexEntry,
        ) -> list[PolicyEvaluationResult]:
            del request, attribution
            time.sleep(0.005)
            return []

        monkeypatch.setattr(interceptor, "_evaluate_policies", delayed_eval)
        request = self._make_request(service_name="svc-policy-timeout")
        result = await interceptor.intercept(request)

        assert result.outcome == InterceptionOutcome.TIMEOUT
        assert result.enrichment_headers["X-ACI-Fail-Open-Reason"] == "POLICY_TIMEOUT"


class _RecordingEventBus:
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> bool:
        self.events.append(event)
        return True


@pytest.mark.asyncio
async def test_active_intervention_emits_audit_event() -> None:
    store = AttributionIndexStore()
    store.materialize(
        AttributionIndexEntry(
            workload_id="svc-route",
            team_id="team-opt",
            team_name="Optimization",
            cost_center_id="CC-3300",
            confidence=0.96,
            confidence_tier="chargeback_ready",
            method_used="R1",
            approved_alternatives=["gpt-4o-mini"],
        )
    )
    bus = _RecordingEventBus()
    interceptor = FailOpenInterceptor(
        store,
        mode=DeploymentMode.ACTIVE,
        event_bus=bus,
    )
    request = InterceptionRequest(
        request_id="req-route-1",
        model="gpt-4o",
        service_name="svc-route",
        estimated_cost_usd=0.0042,
        metadata={"environment": "staging"},
    )

    result = await interceptor.intercept(request)
    await interceptor.shutdown()
    assert result.outcome == InterceptionOutcome.REDIRECTED
    assert result.redirect_model == "gpt-4o-mini"
    assert len(bus.events) == 2
    event_types = {event.event_type for event in bus.events}
    assert EventType.POLICY_EVALUATED in event_types
    assert EventType.INTERVENTION_APPLIED in event_types
    intervention_event = next(
        event for event in bus.events if event.event_type == EventType.INTERVENTION_APPLIED
    )
    assert intervention_event.attributes["action_type"] == "MODEL_ROUTE"
    assert intervention_event.attributes["original_model"] == "gpt-4o"
    assert intervention_event.attributes["new_model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_policy_evaluation_emits_audit_event() -> None:
    store = AttributionIndexStore()
    store.materialize(
        AttributionIndexEntry(
            workload_id="svc-policy-audit",
            team_id="team-audit",
            team_name="Audit",
            cost_center_id="CC-5500",
            confidence=0.91,
            confidence_tier="chargeback_ready",
            method_used="R2",
            model_allowlist=["gpt-4o-mini"],
        )
    )
    bus = _RecordingEventBus()
    interceptor = FailOpenInterceptor(
        store,
        mode=DeploymentMode.ADVISORY,
        event_bus=bus,
    )
    request = InterceptionRequest(
        request_id="req-policy-audit",
        model="gpt-4o",
        service_name="svc-policy-audit",
        metadata={"environment": "staging", "tenant_id": "tenant-alpha"},
    )

    result = await interceptor.intercept(request)
    await interceptor.shutdown()

    assert result.outcome == InterceptionOutcome.SOFT_STOPPED
    policy_event = next(
        event for event in bus.events if event.event_type == EventType.POLICY_EVALUATED
    )
    assert policy_event.tenant_id == "tenant-alpha"
    assert policy_event.attributes["violation_count"] == 1
    assert policy_event.attributes["violated_policy_ids"] == ["model_allowlist"]
