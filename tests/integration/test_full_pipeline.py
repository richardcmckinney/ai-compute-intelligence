"""
Integration tests: end-to-end pipeline validation.

These tests exercise the full data flow from Patent Spec Section 3.3:
  Ingestion -> Reconciliation -> Attribution -> Materialization -> Interception

Each test corresponds to a worked example from Section 13 of the patent spec.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aci.confidence.calibration import CalibrationEngine
from aci.config import PlatformConfig
from aci.core.event_bus import InMemoryEventBus
from aci.graph.store import GraphStore
from aci.hre.engine import HeuristicReconciliationEngine, ReconciliationContext
from aci.index.materializer import AttributionIndexStore, IndexMaterializer
from aci.interceptor.gateway import (
    DeploymentMode,
    FailOpenInterceptor,
    InterceptionOutcome,
    InterceptionRequest,
)
from aci.models.attribution import AttributionIndexEntry
from aci.models.events import DomainEvent, EventType
from aci.models.graph import EdgeProvenance, EdgeType, GraphEdge, GraphNode, NodeType
from aci.trac.calculator import TRACCalculator

NOW = datetime(2026, 2, 8, 14, 23, 0, tzinfo=UTC)


@pytest.mark.integration
class TestFullPipelineExample131:
    """
    Section 13.1: Inference event attributed to team via deterministic path.

    Full path: InferenceEvent -> Model -> CloudResource -> Deployment ->
    Repository -> Person -> Team -> CostCenter.
    Confidence: 1.0 (all R1 direct matches).
    """

    @pytest.fixture(autouse=True)
    def setup_pipeline(self) -> None:
        self.config = PlatformConfig(tenant_id="test", environment="test")
        self.event_bus = InMemoryEventBus()
        self.graph = GraphStore()
        self.index = AttributionIndexStore()
        self.materializer = IndexMaterializer(self.index, self.config)
        self.hre = HeuristicReconciliationEngine()
        self.calibration = CalibrationEngine(self.config.confidence)
        self.trac = TRACCalculator(self.config.trac, self.config.confidence)
        self.interceptor = FailOpenInterceptor(
            self.index,
            self.config.interceptor,
            DeploymentMode.ADVISORY,
        )
        self._build_graph()

    def _build_graph(self) -> None:
        """Populate graph with the Section 13.1 scenario."""
        nodes = [
            GraphNode(
                node_id="endpoint:fraud-v2", node_type=NodeType.INFERENCE_ENDPOINT, label="fraud-v2"
            ),
            GraphNode(
                node_id="deploy:fraud-42",
                node_type=NodeType.DEPLOYMENT,
                label="deploy-fraud-model-prod",
            ),
            GraphNode(
                node_id="repo:co/fraud-model-v2",
                node_type=NodeType.REPOSITORY,
                label="fraud-model-v2",
            ),
            GraphNode(
                node_id="person:jdoe@company.com", node_type=NodeType.PERSON, label="Jane Doe"
            ),
            GraphNode(node_id="team:ML-Fraud", node_type=NodeType.TEAM, label="ML-Fraud"),
            GraphNode(node_id="cc:CC-4521", node_type=NodeType.COST_CENTER, label="CC-4521"),
        ]
        for n in nodes:
            self.graph.upsert_node(n)

        t0 = NOW - timedelta(days=7)
        prov = EdgeProvenance(source="cicd", method="R1")
        for from_id, to_id, etype in [
            ("deploy:fraud-42", "endpoint:fraud-v2", EdgeType.TRIGGERS),
            ("person:jdoe@company.com", "deploy:fraud-42", EdgeType.DEPLOYED_BY),
            ("team:ML-Fraud", "repo:co/fraud-model-v2", EdgeType.OWNS_CODE),
            ("person:jdoe@company.com", "team:ML-Fraud", EdgeType.MEMBER_OF),
            ("team:ML-Fraud", "cc:CC-4521", EdgeType.BUDGETED_UNDER),
        ]:
            self.graph.add_edge(
                GraphEdge(
                    edge_type=etype,
                    from_id=from_id,
                    to_id=to_id,
                    confidence=1.0,
                    weight=1.0,
                    valid_from=t0,
                    provenance=prov,
                )
            )

    def test_r1_deterministic_attribution(self) -> None:
        """HRE resolves the endpoint to ML-Fraud via deterministic path."""
        ctx = ReconciliationContext()
        ctx.identity_mappings = {"endpoint:fraud-v2": "team:ML-Fraud"}

        result = self.hre.resolve(
            entity_id="endpoint:fraud-v2",
            entity_type="inference_endpoint",
            event_time=NOW,
            context=ctx,
        )

        assert result.combined_confidence == 1.0
        assert result.explanation is not None
        assert result.explanation.target_entity == "team:ML-Fraud"

    def test_materialization_produces_index_entry(self) -> None:
        """Materialization creates a compact index entry with O(1) lookup."""
        ctx = ReconciliationContext()
        ctx.identity_mappings = {"endpoint:fraud-v2": "team:ML-Fraud"}

        result = self.hre.resolve("endpoint:fraud-v2", "endpoint", NOW, ctx)
        entry = self.materializer.materialize_attribution(result)

        assert entry.workload_id == "endpoint:fraud-v2"
        assert entry.team_id == "team:ML-Fraud"
        assert entry.confidence == 1.0
        assert entry.confidence_tier == "chargeback_ready"

        looked_up = self.index.lookup("endpoint:fraud-v2")
        assert looked_up is not None
        assert looked_up.team_id == "team:ML-Fraud"

    @pytest.mark.asyncio
    async def test_interception_enriches_headers(self) -> None:
        """Interceptor enriches response headers for attributed workload."""
        self.index.materialize(
            AttributionIndexEntry(
                workload_id="fraud-v2",
                team_id="team:ML-Fraud",
                team_name="ML-Fraud",
                cost_center_id="cc:CC-4521",
                confidence=0.95,
                confidence_tier="chargeback_ready",
                method_used="R1",
                budget_remaining_usd=4200.0,
                budget_limit_usd=10000.0,
            )
        )

        request = InterceptionRequest(
            request_id="req-001",
            model="gpt-4o",
            provider="openai",
            service_name="fraud-v2",
            estimated_cost_usd=4.80,
        )

        result = await self.interceptor.intercept(request)

        assert result.outcome == InterceptionOutcome.ENRICHED
        assert result.elapsed_ms < 50
        assert result.enrichment_headers["X-Attribution-Team"] == "ML-Fraud"
        assert "X-Budget-Remaining-Pct" in result.enrichment_headers

    def test_trac_computation(self) -> None:
        """TRAC computes correctly for the Section 13.1 inference event."""
        result = self.trac.compute(
            workload_id="endpoint:fraud-v2",
            billed_cost_usd=4.80,
            emissions_kg_co2e=0.0004,
            attribution_confidence=1.0,
        )

        assert result.billed_cost_usd == 4.80
        assert result.confidence_risk_premium_usd == 0.0
        assert result.carbon_liability_usd < 0.01
        assert result.trac_usd == pytest.approx(4.80, abs=0.05)


@pytest.mark.integration
class TestFullPipelineExample132:
    """
    Section 13.2: Interceptor gateway decision for customer-support-bot.
    """

    @pytest.fixture(autouse=True)
    def setup_pipeline(self) -> None:
        self.index = AttributionIndexStore()
        self.interceptor = FailOpenInterceptor(
            self.index,
            mode=DeploymentMode.ADVISORY,
        )
        self.index.materialize(
            AttributionIndexEntry(
                workload_id="customer-support-bot",
                team_id="team:CS-Platform",
                team_name="CS-Platform",
                cost_center_id="cc:CC-1200",
                confidence=0.95,
                confidence_tier="chargeback_ready",
                method_used="R1",
                budget_remaining_usd=1800.0,
                budget_limit_usd=5000.0,
                model_allowlist=["gpt-4o-mini", "gpt-4o", "claude-3-haiku"],
                equivalence_class_id="customer-support-chat",
                approved_alternatives=["gpt-4o-mini", "claude-3-haiku"],
            )
        )

    @pytest.mark.asyncio
    async def test_advisory_mode_enrichment(self) -> None:
        """Section 13.2: Request enriched with cost, carbon, budget headers."""
        request = InterceptionRequest(
            request_id="req-cs-001",
            model="gpt-4o-mini",
            provider="openai",
            service_name="customer-support-bot",
            estimated_cost_usd=0.0012,
        )

        result = await self.interceptor.intercept(request)

        assert result.outcome == InterceptionOutcome.ENRICHED
        assert result.elapsed_ms < 50
        assert result.attribution is not None
        assert result.attribution.team_name == "CS-Platform"
        assert result.enrichment_headers["X-Budget-Remaining-Pct"] == "36"

    @pytest.mark.asyncio
    async def test_fail_open_on_unknown_workload(self) -> None:
        """Unknown workload: interceptor fails open, logs shadow event."""
        request = InterceptionRequest(
            request_id="req-unknown",
            model="gpt-4o",
            provider="openai",
            service_name="unknown-service",
        )

        result = await self.interceptor.intercept(request)

        assert result.outcome == InterceptionOutcome.FAIL_OPEN
        assert result.shadow_event_logged is True


@pytest.mark.integration
class TestFullPipelineExample133:
    """
    Section 13.3: Dirty data with probabilistic attribution.

    New SageMaker endpoint 'nlp-experiment-7' with no tags, no CI/CD linkage.
    Resolved via R2 (temporal correlation) + R3 (naming convention).
    """

    @pytest.fixture(autouse=True)
    def setup_pipeline(self) -> None:
        self.hre = HeuristicReconciliationEngine()

    def test_probabilistic_attribution(self) -> None:
        """R2 + R3 produce combined confidence (provisional range)."""
        ctx = ReconciliationContext()
        ctx.temporal_events = [
            (NOW - timedelta(minutes=5), "person:asmith@company.com", "jupyter"),
        ]
        ctx.naming_patterns = {
            "team:NLP-Research": ["nlp-experiment", "nlp-pipeline", "nlp-eval"],
            "team:Data-Engineering": ["data-pipeline", "etl-job"],
        }

        result = self.hre.resolve(
            entity_id="nlp-experiment-7",
            entity_type="sagemaker_endpoint",
            event_time=NOW,
            context=ctx,
        )

        assert result.combined_confidence > 0.0
        assert result.explanation is not None

        signals = result.explanation.top_contributing_signals
        methods_used = {s["method"] for s in signals}
        assert len(methods_used) >= 1

    def test_provisional_tier_assignment(self) -> None:
        """Confidence between 0.50 and 0.79 is classified as provisional."""
        engine = CalibrationEngine()
        tier = engine.get_confidence_tier(0.78)
        assert tier.value == "provisional"

    def test_explanation_artifact_completeness(self) -> None:
        """Explanation artifact contains all required fields per Section 5.4."""
        ctx = ReconciliationContext()
        ctx.temporal_events = [
            (NOW - timedelta(minutes=5), "person:asmith@company.com", "jupyter"),
        ]
        ctx.naming_patterns = {"team:NLP-Research": ["nlp-experiment"]}

        result = self.hre.resolve("nlp-experiment-7", "endpoint", NOW, ctx)

        explanation = result.explanation
        assert explanation is not None
        assert explanation.attribution_id != ""
        assert explanation.target_entity != ""
        assert explanation.confidence_score >= 0.0
        assert explanation.method_used != ""
        assert isinstance(explanation.top_contributing_signals, list)
        assert isinstance(explanation.feature_values, dict)
        assert isinstance(explanation.alternatives_considered, list)


@pytest.mark.integration
class TestEventSourcingReplay:
    """Validate event sourcing and replay (Section 3.2)."""

    @pytest.fixture(autouse=True)
    def setup_pipeline(self) -> None:
        self.bus = InMemoryEventBus()

    @pytest.mark.asyncio
    async def test_idempotency_deduplication(self) -> None:
        """Duplicate events are deduplicated by idempotency_key."""
        event = DomainEvent(
            event_type=EventType.INFERENCE_REQUEST,
            subject_id="req-1",
            attributes={"model": "gpt-4o", "provider": "openai"},
            event_time=NOW,
            source="test",
            idempotency_key="test:req-1",
            tenant_id="test",
        )

        first = await self.bus.publish(event)
        second = await self.bus.publish(event)

        assert first is True
        assert second is False
        assert self.bus.stats["published"] == 1
        assert self.bus.stats["deduplicated"] == 1

    @pytest.mark.asyncio
    async def test_replay_with_time_filter(self) -> None:
        """Replay returns events within the specified time window."""
        for i in range(5):
            event = DomainEvent(
                event_type=EventType.BILLING_LINE_ITEM,
                subject_id=f"item-{i}",
                attributes={
                    "cloud_provider": "aws",
                    "account_id": "111111111111",
                    "service": "bedrock",
                    "cost_usd": i * 10.0,
                },
                event_time=NOW - timedelta(hours=5 - i),
                source="test",
                idempotency_key=f"test:item-{i}",
                tenant_id="test",
            )
            await self.bus.publish(event)

        replayed = self.bus.replay(
            from_time=NOW - timedelta(hours=3),
            to_time=NOW,
        )

        assert len(replayed) == 3


@pytest.mark.integration
class TestTemporalDecayIntegration:
    """Validate TRAC temporal decay (Section 7)."""

    def test_fresh_vs_stale_trac(self) -> None:
        """Stale attribution produces higher TRAC due to confidence decay."""
        trac = TRACCalculator()

        fresh = trac.compute(
            workload_id="w1",
            billed_cost_usd=100.0,
            emissions_kg_co2e=0.5,
            attribution_confidence=0.85,
            signal_age_days=10.0,
        )

        stale = trac.compute(
            workload_id="w1",
            billed_cost_usd=100.0,
            emissions_kg_co2e=0.5,
            attribution_confidence=0.85,
            signal_age_days=180.0,
        )

        assert stale.confidence_risk_premium_usd > fresh.confidence_risk_premium_usd
        assert stale.trac_usd > fresh.trac_usd
