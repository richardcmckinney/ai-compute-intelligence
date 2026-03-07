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
    historical_edges = [edge for edge in store.edges if edge.valid_to is not None]
    active_edges = [edge for edge in store.edges if edge.valid_to is None]
    assert len(historical_edges) == 1
    assert len(active_edges) == 1

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


def test_get_nodes_by_type_uses_type_index() -> None:
    store = GraphStore()
    store.upsert_node(GraphNode(node_id="team:1", node_type=NodeType.TEAM, label="Team 1"))
    store.upsert_node(
        GraphNode(node_id="repo:1", node_type=NodeType.REPOSITORY, label="repo-one")
    )
    store.upsert_node(GraphNode(node_id="team:2", node_type=NodeType.TEAM, label="Team 2"))

    team_ids = {node.node_id for node in store.get_nodes_by_type(NodeType.TEAM)}
    assert team_ids == {"team:1", "team:2"}


def test_traverse_attribution_respects_max_paths() -> None:
    store = GraphStore()
    now = datetime.now(UTC)

    store.upsert_node(
        GraphNode(node_id="endpoint:svc", node_type=NodeType.INFERENCE_ENDPOINT, label="svc")
    )
    for idx in range(3):
        team_id = f"team:{idx}"
        cc_id = f"cc:{idx}"
        store.upsert_node(GraphNode(node_id=team_id, node_type=NodeType.TEAM, label=team_id))
        store.upsert_node(GraphNode(node_id=cc_id, node_type=NodeType.COST_CENTER, label=cc_id))
        store.add_edge(
            GraphEdge(
                edge_type=EdgeType.ATTRIBUTED_TO,
                from_id="endpoint:svc",
                to_id=team_id,
                confidence=1.0,
                weight=1.0,
                valid_from=now,
                provenance=EdgeProvenance(source="unit", method="R1"),
            )
        )
        store.add_edge(
            GraphEdge(
                edge_type=EdgeType.REPORTS_TO,
                from_id=team_id,
                to_id=cc_id,
                confidence=1.0,
                weight=1.0,
                valid_from=now,
                provenance=EdgeProvenance(source="unit", method="R1"),
            )
        )

    paths = store.traverse_attribution("endpoint:svc", max_depth=4, max_paths=2)
    assert len(paths) == 2
