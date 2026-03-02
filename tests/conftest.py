"""
Shared test fixtures for the ACI platform test suite.

Provides pre-configured instances of all core engines, sample data
matching the patent specification worked examples (Section 13),
and utility factories for common test scenarios.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aci.config import PlatformConfig
from aci.confidence.calibration import CalibrationEngine
from aci.core.event_bus import InMemoryEventBus
from aci.equivalence.verifier import EquivalenceClass, EquivalenceVerifier
from aci.graph.store import GraphStore
from aci.hre.engine import HeuristicReconciliationEngine, ReconciliationContext
from aci.index.materializer import AttributionIndexStore, IndexMaterializer
from aci.interceptor.gateway import DeploymentMode, FailOpenInterceptor
from aci.models.attribution import AttributionIndexEntry
from aci.models.events import DomainEvent, EventType, InferenceEvent
from aci.models.graph import EdgeProvenance, EdgeType, GraphEdge, GraphNode, NodeType
from aci.policy.engine import PolicyEngine
from aci.trac.calculator import TRACCalculator


NOW = datetime(2026, 2, 8, 14, 23, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Configuration fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig(tenant_id="test-tenant", environment="test")


# ---------------------------------------------------------------------------
# Core engine fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def event_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def graph_store() -> GraphStore:
    return GraphStore()


@pytest.fixture
def index_store() -> AttributionIndexStore:
    return AttributionIndexStore()


@pytest.fixture
def hre() -> HeuristicReconciliationEngine:
    return HeuristicReconciliationEngine()


@pytest.fixture
def calibration(config: PlatformConfig) -> CalibrationEngine:
    return CalibrationEngine(config.confidence)


@pytest.fixture
def trac_calculator(config: PlatformConfig) -> TRACCalculator:
    return TRACCalculator(config.trac, config.confidence)


@pytest.fixture
def policy_engine() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def equivalence_verifier(config: PlatformConfig) -> EquivalenceVerifier:
    verifier = EquivalenceVerifier(config.equivalence)
    # Register a sample equivalence class (Section 6.2 example).
    verifier.register_class(EquivalenceClass(
        class_id="customer-support-chat",
        name="Customer Support Chat",
        approved_models=["gpt-4o", "gpt-4o-mini", "claude-3-haiku"],
        use_case="Customer-facing chat support",
    ))
    return verifier


@pytest.fixture
def interceptor(index_store: AttributionIndexStore) -> FailOpenInterceptor:
    return FailOpenInterceptor(
        index=index_store,
        mode=DeploymentMode.ADVISORY,
    )


@pytest.fixture
def materializer(
    index_store: AttributionIndexStore,
    config: PlatformConfig,
) -> IndexMaterializer:
    return IndexMaterializer(index_store, config)


# ---------------------------------------------------------------------------
# Sample data: worked example from Patent Spec Section 13.1
# ---------------------------------------------------------------------------

@pytest.fixture
def fraud_team_graph(graph_store: GraphStore) -> GraphStore:
    """
    Build the attribution graph from Section 13.1 worked example:
    InferenceEvent -> Model -> CloudResource -> Deployment -> Repo -> Person -> Team -> CostCenter
    """
    nodes = [
        GraphNode(node_id="model:gpt-4o", node_type=NodeType.MODEL, label="gpt-4o"),
        GraphNode(
            node_id="resource:fraud-v2",
            node_type=NodeType.CLOUD_RESOURCE,
            label="fraud-v2",
            properties={"arn": "arn:aws:sagemaker:us-east-1:...:endpoint/fraud-v2"},
        ),
        GraphNode(node_id="deploy:fraud-prod-42", node_type=NodeType.DEPLOYMENT, label="deploy-fraud-model-prod"),
        GraphNode(node_id="repo:co/fraud-model-v2", node_type=NodeType.REPOSITORY, label="fraud-model-v2"),
        GraphNode(node_id="person:jdoe@company.com", node_type=NodeType.PERSON, label="Jane Doe"),
        GraphNode(node_id="team:ml-fraud", node_type=NodeType.TEAM, label="ML-Fraud"),
        GraphNode(node_id="cc:CC-4521", node_type=NodeType.COST_CENTER, label="CC-4521"),
    ]
    for n in nodes:
        graph_store.upsert_node(n)

    edges = [
        ("resource:fraud-v2", "model:gpt-4o", EdgeType.INVOKES),
        ("deploy:fraud-prod-42", "resource:fraud-v2", EdgeType.TRIGGERS),
        ("person:jdoe@company.com", "deploy:fraud-prod-42", EdgeType.DEPLOYED_BY),
        ("person:jdoe@company.com", "repo:co/fraud-model-v2", EdgeType.OWNS_CODE),
        ("team:ml-fraud", "repo:co/fraud-model-v2", EdgeType.OWNS_CODE),
        ("person:jdoe@company.com", "team:ml-fraud", EdgeType.MEMBER_OF),
        ("team:ml-fraud", "cc:CC-4521", EdgeType.BUDGETED_UNDER),
    ]
    for from_id, to_id, etype in edges:
        graph_store.add_edge(GraphEdge(
            edge_type=etype,
            from_id=from_id,
            to_id=to_id,
            confidence=1.0,
            weight=1.0,
            valid_from=NOW - timedelta(days=30),
            provenance=EdgeProvenance(source="cicd", method="R1"),
        ))

    return graph_store


@pytest.fixture
def sample_index_entry() -> AttributionIndexEntry:
    """Sample index entry for the customer-support-bot (Section 13.2)."""
    return AttributionIndexEntry(
        workload_id="customer-support-bot",
        team_id="team:cs-platform",
        team_name="CS-Platform",
        cost_center_id="cc:CC-1200",
        cost_center_name="CC-1200",
        service_name="customer-support-bot",
        confidence=0.92,
        confidence_tier="chargeback_ready",
        method_used="R1",
        budget_remaining_usd=1800.0,
        budget_limit_usd=5000.0,
        model_allowlist=["gpt-4o-mini", "gpt-4o", "claude-3-haiku"],
        equivalence_class_id="customer-support-chat",
        approved_alternatives=["gpt-4o-mini", "claude-3-haiku"],
    )


@pytest.fixture
def populated_index(
    index_store: AttributionIndexStore,
    sample_index_entry: AttributionIndexEntry,
) -> AttributionIndexStore:
    """Index store pre-populated with the sample entry."""
    index_store.materialize(sample_index_entry)
    return index_store


# ---------------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------------

def make_inference_event(
    model: str = "gpt-4o",
    cost_usd: float = 4.80,
    tokens: int = 1_200_000,
    service_name: str = "fraud-v2",
) -> DomainEvent:
    """Create a sample inference event matching Section 13.1."""
    return DomainEvent(
        event_type=EventType.INFERENCE_REQUEST,
        subject_id=f"req:{service_name}",
        attributes=InferenceEvent(
            model=model,
            provider="openai",
            cloud_resource_arn=f"arn:aws:sagemaker:us-east-1:...:endpoint/{service_name}",
            region="us-east-1",
            input_tokens=tokens,
            output_tokens=0,
            cost_usd=cost_usd,
            service_name=service_name,
        ).model_dump(),
        event_time=NOW,
        source="aws-bedrock",
        idempotency_key=f"test:{service_name}:1",
        tenant_id="test-tenant",
    )
