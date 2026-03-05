from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aci.graph.store import GraphStore
from aci.models.graph import EdgeProvenance, EdgeType, GraphEdge, GraphNode, NodeType


def test_add_edge_closes_previous_active_edge() -> None:
    store = GraphStore()
    now = datetime.now(UTC)

    store.upsert_node(
        GraphNode(
            node_id="svc:a",
            node_type=NodeType.INFERENCE_ENDPOINT,
            label="svc-a",
        )
    )
    store.upsert_node(GraphNode(node_id="team:x", node_type=NodeType.TEAM, label="Team X"))

    first = GraphEdge(
        edge_type=EdgeType.ATTRIBUTED_TO,
        from_id="team:x",
        to_id="svc:a",
        confidence=0.9,
        weight=1.0,
        valid_from=now - timedelta(days=2),
        provenance=EdgeProvenance(source="unit", method="R1"),
    )
    second = GraphEdge(
        edge_type=EdgeType.ATTRIBUTED_TO,
        from_id="team:x",
        to_id="svc:a",
        confidence=1.0,
        weight=1.0,
        valid_from=now - timedelta(days=1),
        provenance=EdgeProvenance(source="unit", method="R1"),
    )

    store.add_edge(first)
    store.add_edge(second)

    assert len(store.edges) == 2
    assert store.edges[0].valid_to is not None
    assert store.edges[1].valid_to is None

    stats = store.get_stats()
    assert stats["active_edges"] == 1
    assert stats["historical_edges"] == 1


def test_get_edges_from_uses_indexed_subset_and_time_filter() -> None:
    store = GraphStore()
    now = datetime.now(UTC)

    store.upsert_node(
        GraphNode(
            node_id="svc:1",
            node_type=NodeType.INFERENCE_ENDPOINT,
            label="svc-1",
        )
    )
    store.upsert_node(
        GraphNode(
            node_id="svc:2",
            node_type=NodeType.INFERENCE_ENDPOINT,
            label="svc-2",
        )
    )
    store.upsert_node(GraphNode(node_id="team:1", node_type=NodeType.TEAM, label="Team 1"))

    edge_old = GraphEdge(
        edge_type=EdgeType.ATTRIBUTED_TO,
        from_id="team:1",
        to_id="svc:1",
        confidence=1.0,
        weight=1.0,
        valid_from=now - timedelta(days=10),
        valid_to=now - timedelta(days=5),
        provenance=EdgeProvenance(source="unit", method="R1"),
    )
    edge_active = GraphEdge(
        edge_type=EdgeType.ATTRIBUTED_TO,
        from_id="team:1",
        to_id="svc:2",
        confidence=1.0,
        weight=1.0,
        valid_from=now - timedelta(days=1),
        provenance=EdgeProvenance(source="unit", method="R1"),
    )

    store.add_edge(edge_old)
    store.add_edge(edge_active)

    past_edges = store.get_edges_from("team:1", at_time=now - timedelta(days=7))
    current_edges = store.get_edges_from("team:1", at_time=now)

    assert len(past_edges) == 1
    assert past_edges[0].to_id == "svc:1"
    assert len(current_edges) == 1
    assert current_edges[0].to_id == "svc:2"
