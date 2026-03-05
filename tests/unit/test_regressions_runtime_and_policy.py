from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aci.config import AuthConfig, PlatformConfig
from aci.confidence.calibration import CalibrationEngine
from aci.core.event_bus import InMemoryEventBus
from aci.core.processor import AttributionProcessor
from aci.graph.store import GraphStore
from aci.hre.engine import HeuristicReconciliationEngine
from aci.index.materializer import AttributionIndexStore, IndexMaterializer
from aci.models.carbon import EnforcementAction, PolicyDefinition, PolicyType
from aci.models.graph import EdgeProvenance, EdgeType, GraphEdge, GraphNode, NodeType
from aci.policy.engine import PolicyEngine


def test_build_context_supports_namespaced_resource_node_ids() -> None:
    graph = GraphStore()
    event_bus = InMemoryEventBus()
    materializer = IndexMaterializer(AttributionIndexStore())
    policy_engine = PolicyEngine()
    processor = AttributionProcessor(
        event_bus=event_bus,
        graph_store=graph,
        hre=HeuristicReconciliationEngine(),
        calibration=CalibrationEngine(),
        materializer=materializer,
        policy_engine=policy_engine,
    )

    team = GraphNode(node_id="team:ml-core", node_type=NodeType.TEAM, label="ML Core")
    graph.upsert_node(team)
    graph.add_edge(
        GraphEdge(
            edge_type=EdgeType.ATTRIBUTED_TO,
            from_id="resource:arn:aws:sagemaker:us-east-1:123:endpoint/fraud-v2",
            to_id=team.node_id,
            confidence=1.0,
            weight=1.0,
            valid_from=datetime.now(UTC),
            provenance=EdgeProvenance(source="test", method="R1"),
        )
    )

    ctx = processor._build_context(
        workload_id="arn:aws:sagemaker:us-east-1:123:endpoint/fraud-v2",
        event_time=datetime.now(UTC),
        tenant_id="tenant-a",
    )
    assert ctx.identity_mappings[
        "arn:aws:sagemaker:us-east-1:123:endpoint/fraud-v2"
    ] == "team:ml-core"


def test_policy_engine_applies_team_specific_over_global() -> None:
    engine = PolicyEngine()

    engine.register_policy(
        PolicyDefinition(
            policy_id="allowlist-global",
            policy_type=PolicyType.MODEL_ALLOWLIST,
            name="Global Allowlist",
            scope="global",
            enforcement=EnforcementAction.SOFT_STOP,
            parameters={"allowed_models": ["gpt-4o-mini"]},
        )
    )
    engine.register_policy(
        PolicyDefinition(
            policy_id="allowlist-team-a",
            policy_type=PolicyType.MODEL_ALLOWLIST,
            name="Team A Allowlist",
            scope="team-a",
            enforcement=EnforcementAction.SOFT_STOP,
            parameters={"allowed_models": ["claude-3-haiku"]},
        )
    )

    assert engine.get_model_allowlist("team-a") == ["claude-3-haiku"]
    assert engine.get_model_allowlist("team-b") == ["gpt-4o-mini"]


def test_policy_engine_token_budgets_are_team_scoped() -> None:
    engine = PolicyEngine()
    engine.register_policy(
        PolicyDefinition(
            policy_id="tokens-global",
            policy_type=PolicyType.TOKEN_BUDGET,
            name="Global Token Budget",
            scope="global",
            enforcement=EnforcementAction.SOFT_STOP,
            parameters={"max_output_tokens": 1000, "max_input_tokens": 2000},
        )
    )
    engine.register_policy(
        PolicyDefinition(
            policy_id="tokens-team-a",
            policy_type=PolicyType.TOKEN_BUDGET,
            name="Team A Token Budget",
            scope="team-a",
            enforcement=EnforcementAction.SOFT_STOP,
            parameters={"max_output_tokens": 250, "max_input_tokens": 500},
        )
    )

    assert engine.get_token_budgets("team-a") == {
        "token_budget_output": 250,
        "token_budget_input": 500,
    }
    assert engine.get_token_budgets("team-b") == {
        "token_budget_output": 1000,
        "token_budget_input": 2000,
    }


def test_platform_config_rejects_disabled_auth_in_production() -> None:
    with pytest.raises(ValueError, match="auth.enabled must be true"):
        PlatformConfig(
            environment="production",
            neo4j_password="strong-secret",
            auth=AuthConfig(
                enabled=False,
                allow_dev_bypass=False,
                jwt_algorithm="HS256",
                jwt_hs256_secret="really-strong-secret",
            ),
        )
