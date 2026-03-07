"""Deterministic demo bootstrap for local and reviewer-facing walkthroughs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aci.models.attribution import AttributionIndexEntry
from aci.models.events import DomainEvent, EventType
from aci.models.graph import EdgeType, GraphEdge, GraphNode, NodeType

if TYPE_CHECKING:
    from aci.api.runtime import AppState


@dataclass(frozen=True)
class DemoBootstrapResult:
    seeded_entries: int
    workloads: list[str]
    events_published: int
    graph_nodes: int
    graph_edges: int


def demo_seed_entries() -> list[AttributionIndexEntry]:
    """Stable index entries used by the API demo and investor walkthroughs."""
    return [
        AttributionIndexEntry(
            workload_id="customer-support-bot",
            team_id="team-cs-platform",
            team_name="Customer Support Platform",
            cost_center_id="CC-1200",
            confidence=0.96,
            confidence_tier="chargeback_ready",
            method_used="R1",
            budget_remaining_usd=1850.0,
            budget_limit_usd=5000.0,
            model_allowlist=["gpt-4o-mini", "gemini-1.5-flash", "gpt-4o"],
            approved_alternatives=["gpt-4o-mini", "gemini-1.5-flash"],
            token_budget_output=800,
            token_budget_input=2400,
            cost_ceiling_per_request_usd=0.025,
        ),
        AttributionIndexEntry(
            workload_id="analytics-batch",
            team_id="team-data-science",
            team_name="Data Science",
            cost_center_id="CC-5100",
            confidence=0.93,
            confidence_tier="chargeback_ready",
            method_used="R2",
            budget_remaining_usd=620.0,
            budget_limit_usd=10000.0,
            model_allowlist=["gpt-4o", "gpt-4o-mini"],
            token_budget_output=1200,
            token_budget_input=3000,
            cost_ceiling_per_request_usd=0.008,
        ),
        AttributionIndexEntry(
            workload_id="code-intel-prod",
            team_id="team-platform-eng",
            team_name="Platform Engineering",
            cost_center_id="CC-3100",
            confidence=0.95,
            confidence_tier="chargeback_ready",
            method_used="R1",
            budget_remaining_usd=2250.0,
            budget_limit_usd=7000.0,
            model_allowlist=["gemini-2.0-flash", "gemini-1.5-flash"],
            approved_alternatives=["gemini-1.5-flash"],
            token_budget_output=1500,
            token_budget_input=3500,
            cost_ceiling_per_request_usd=0.03,
        ),
    ]


async def bootstrap_demo_state(
    app_state: AppState,
    *,
    reset_existing: bool = True,
) -> DemoBootstrapResult:
    """Seed deterministic graph and index state for demo/profile runs."""
    existing_size = app_state.index_store.size
    if not reset_existing and existing_size > 0:
        graph_stats = app_state.graph.get_stats() if app_state.graph is not None else {
            "total_nodes": 0,
            "total_edges": 0,
        }
        current_workloads = [entry.workload_id for entry in demo_seed_entries()]
        return DemoBootstrapResult(
            seeded_entries=existing_size,
            workloads=current_workloads,
            events_published=0,
            graph_nodes=graph_stats["total_nodes"],
            graph_edges=graph_stats["total_edges"],
        )

    if reset_existing:
        app_state.index_store.clear()
        if app_state.graph is not None:
            app_state.graph.clear()

    if app_state.graph is not None:
        _seed_graph(app_state)

    events_published = 0
    if app_state.accepts_ingestion and app_state.config.event_bus_backend.lower() == "memory":
        result = await app_state.event_bus.publish_batch(_demo_events(app_state))
        events_published = result["published"]

    entries = demo_seed_entries()
    for entry in entries:
        app_state.index_store.materialize(entry)

    graph_stats = app_state.graph.get_stats() if app_state.graph is not None else {
        "total_nodes": 0,
        "total_edges": 0,
    }
    return DemoBootstrapResult(
        seeded_entries=len(entries),
        workloads=[entry.workload_id for entry in entries],
        events_published=events_published,
        graph_nodes=graph_stats["total_nodes"],
        graph_edges=graph_stats["total_edges"],
    )


def _seed_graph(app_state: AppState) -> None:
    if app_state.graph is None:
        return

    seeded_at = datetime.now(UTC)
    nodes = [
        GraphNode(
            node_id="team:team-cs-platform",
            node_type=NodeType.TEAM,
            label="Customer Support Platform",
        ),
        GraphNode(
            node_id="team:team-data-science",
            node_type=NodeType.TEAM,
            label="Data Science",
        ),
        GraphNode(
            node_id="team:team-platform-eng",
            node_type=NodeType.TEAM,
            label="Platform Engineering",
        ),
        GraphNode(
            node_id="budget:CC-1200",
            node_type=NodeType.BUDGET,
            label="CC-1200",
        ),
        GraphNode(
            node_id="budget:CC-5100",
            node_type=NodeType.BUDGET,
            label="CC-5100",
        ),
        GraphNode(
            node_id="budget:CC-3100",
            node_type=NodeType.BUDGET,
            label="CC-3100",
        ),
        GraphNode(
            node_id="resource:customer-support-bot",
            node_type=NodeType.INFERENCE_ENDPOINT,
            label="customer-support-bot",
        ),
        GraphNode(
            node_id="resource:analytics-batch",
            node_type=NodeType.INFERENCE_ENDPOINT,
            label="analytics-batch",
        ),
        GraphNode(
            node_id="resource:code-intel-prod",
            node_type=NodeType.INFERENCE_ENDPOINT,
            label="code-intel-prod",
        ),
        GraphNode(
            node_id="repo:customer-support-platform",
            node_type=NodeType.REPOSITORY,
            label="customer-support-platform",
        ),
        GraphNode(
            node_id="repo:analytics-batch",
            node_type=NodeType.REPOSITORY,
            label="analytics-batch",
        ),
        GraphNode(
            node_id="repo:code-intelligence",
            node_type=NodeType.REPOSITORY,
            label="code-intelligence",
        ),
        GraphNode(
            node_id="person:alice.ng",
            node_type=NodeType.PERSON,
            label="alice.ng",
        ),
    ]
    for node in nodes:
        app_state.graph.upsert_node(node)

    edges = [
        GraphEdge.deterministic(
            edge_type=EdgeType.ATTRIBUTED_TO,
            from_id="resource:customer-support-bot",
            to_id="team:team-cs-platform",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.ATTRIBUTED_TO,
            from_id="resource:analytics-batch",
            to_id="team:team-data-science",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.ATTRIBUTED_TO,
            from_id="resource:code-intel-prod",
            to_id="team:team-platform-eng",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.OWNS_CODE,
            from_id="team:team-cs-platform",
            to_id="repo:customer-support-platform",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.OWNS_CODE,
            from_id="team:team-data-science",
            to_id="repo:analytics-batch",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.OWNS_CODE,
            from_id="team:team-platform-eng",
            to_id="repo:code-intelligence",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.BUDGETED_UNDER,
            from_id="team:team-cs-platform",
            to_id="budget:CC-1200",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.BUDGETED_UNDER,
            from_id="team:team-data-science",
            to_id="budget:CC-5100",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.BUDGETED_UNDER,
            from_id="team:team-platform-eng",
            to_id="budget:CC-3100",
            valid_from=seeded_at,
            source="demo",
        ),
        GraphEdge.deterministic(
            edge_type=EdgeType.MEMBER_OF,
            from_id="person:alice.ng",
            to_id="team:team-platform-eng",
            valid_from=seeded_at,
            source="demo",
        ),
    ]
    for edge in edges:
        app_state.graph.add_edge(edge)


def _demo_events(app_state: AppState) -> list[DomainEvent]:
    timestamp = datetime.now(UTC)
    tenant_id = app_state.config.tenant_id
    return [
        DomainEvent(
            event_type=EventType.ORG_CHANGE,
            subject_id="person:alice.ng",
            attributes={
                "person_id": "alice.ng",
                "previous_team": "",
                "new_team": "team-platform-eng",
                "effective_date": timestamp,
                "source_system": "demo-seeder",
            },
            event_time=timestamp,
            source="demo-seeder",
            idempotency_key="demo-org-change-alice",
            tenant_id=tenant_id,
        ),
        DomainEvent(
            event_type=EventType.DEPLOYMENT,
            subject_id="deploy:code-intel-prod",
            attributes={
                "service_name": "code-intel-prod",
                "repository": "code-intelligence",
                "commit_sha": "demo123",
                "deployer_identity": "alice.ng",
                "deploy_tool": "github-actions",
                "deploy_job_id": "deploy-code-intel-prod",
                "target_environment": "production",
                "target_resource_arn": "code-intel-prod",
                "timestamp": timestamp,
            },
            event_time=timestamp,
            source="demo-seeder",
            idempotency_key="demo-deployment-code-intel",
            tenant_id=tenant_id,
        ),
        DomainEvent(
            event_type=EventType.BILLING_LINE_ITEM,
            subject_id="billing:analytics-batch",
            attributes={
                "cloud_provider": "aws",
                "account_id": "123456789012",
                "service": "bedrock",
                "resource_arn": "analytics-batch",
                "region": "us-west-2",
                "cost_usd": 183.42,
                "usage_quantity": 1.0,
                "usage_unit": "request",
                "tags": {"team": "team-data-science"},
            },
            event_time=timestamp,
            source="demo-seeder",
            idempotency_key="demo-billing-analytics-batch",
            tenant_id=tenant_id,
        ),
        DomainEvent(
            event_type=EventType.INFERENCE_REQUEST,
            subject_id="req-customer-support-bot",
            attributes={
                "model": "gpt-4o-mini",
                "provider": "openai",
                "service_name": "customer-support-bot",
                "request_id": "req-customer-support-bot",
                "input_tokens": 620,
                "output_tokens": 180,
                "latency_ms": 212.0,
                "cost_usd": 0.0032,
            },
            event_time=timestamp,
            source="demo-seeder",
            idempotency_key="demo-inference-customer-support-bot",
            tenant_id=tenant_id,
        ),
        DomainEvent(
            event_type=EventType.INFERENCE_REQUEST,
            subject_id="req-analytics-batch",
            attributes={
                "model": "gpt-4o",
                "provider": "openai",
                "service_name": "analytics-batch",
                "request_id": "req-analytics-batch",
                "input_tokens": 2600,
                "output_tokens": 900,
                "latency_ms": 481.0,
                "cost_usd": 0.014,
            },
            event_time=timestamp,
            source="demo-seeder",
            idempotency_key="demo-inference-analytics-batch",
            tenant_id=tenant_id,
        ),
        DomainEvent(
            event_type=EventType.INFERENCE_REQUEST,
            subject_id="req-code-intel-prod",
            attributes={
                "model": "gemini-2.0-flash",
                "provider": "google",
                "service_name": "code-intel-prod",
                "request_id": "req-code-intel-prod",
                "input_tokens": 1800,
                "output_tokens": 500,
                "latency_ms": 162.0,
                "cost_usd": 0.009,
            },
            event_time=timestamp,
            source="demo-seeder",
            idempotency_key="demo-inference-code-intel-prod",
            tenant_id=tenant_id,
        ),
    ]
