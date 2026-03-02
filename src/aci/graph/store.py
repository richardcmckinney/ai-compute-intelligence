"""
Graph store: Neo4j-backed attribution graph (Patent Spec Section 3.1, Tier 1).

The authoritative graph store maintains the full property graph with time-versioned
edges. It is updated asynchronously by the reconciliation engine. The interceptor
NEVER reads from this store; it reads only from the attribution index (Tier 2).
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from aci.models.graph import GraphEdge, GraphNode, NodeType

logger = structlog.get_logger()


class GraphStore:
    """
    In-memory graph store for development and testing.

    In production, this is backed by Neo4j with the following schema:
    - Nodes: typed entities with properties.
    - Edges: time-versioned, weighted, with confidence and provenance.
    - Queries: temporal point-in-time attribution path traversal.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []

    def upsert_node(self, node: GraphNode) -> None:
        """Insert or update a node."""
        existing = self.nodes.get(node.node_id)
        if existing:
            node = node.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        self.nodes[node.node_id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        """
        Add a time-versioned edge.

        Edges are append-only: when relationships change, the old edge
        gets a valid_to timestamp and a new edge is created.
        """
        # Close any existing edge of the same type between these nodes.
        now = datetime.now(timezone.utc)
        for existing in self.edges:
            if (
                existing.from_id == edge.from_id
                and existing.to_id == edge.to_id
                and existing.edge_type == edge.edge_type
                and existing.valid_to is None
            ):
                # Close the existing edge.
                idx = self.edges.index(existing)
                self.edges[idx] = existing.model_copy(update={"valid_to": now})

        self.edges.append(edge)

    def get_node(self, node_id: str) -> GraphNode | None:
        return self.nodes.get(node_id)

    def get_edges_from(
        self,
        node_id: str,
        at_time: datetime | None = None,
    ) -> list[GraphEdge]:
        """
        Get all outgoing edges from a node, optionally at a point in time.

        Time-versioned query: returns only edges valid at the specified time.
        """
        at = at_time or datetime.now(timezone.utc)
        return [
            e for e in self.edges
            if e.from_id == node_id
            and e.valid_from <= at
            and (e.valid_to is None or e.valid_to > at)
        ]

    def get_edges_to(
        self,
        node_id: str,
        at_time: datetime | None = None,
    ) -> list[GraphEdge]:
        """Get all incoming edges to a node, optionally at a point in time."""
        at = at_time or datetime.now(timezone.utc)
        return [
            e for e in self.edges
            if e.to_id == node_id
            and e.valid_from <= at
            and (e.valid_to is None or e.valid_to > at)
        ]

    def traverse_attribution(
        self,
        start_node_id: str,
        at_time: datetime | None = None,
        max_depth: int = 8,
    ) -> list[list[GraphEdge]]:
        """
        Traverse the attribution graph from a starting node to cost centers.

        Follows edges toward organizational hierarchy (Team -> CostCenter).
        Returns all paths found, supporting fractional attribution through
        multiple paths.

        This is NOT called at decision time. It runs during the async
        reconciliation phase (Phase 3) to produce attribution results
        that are then materialized into the index.
        """
        paths: list[list[GraphEdge]] = []
        self._dfs(start_node_id, at_time, [], paths, max_depth, set())
        return paths

    def _dfs(
        self,
        node_id: str,
        at_time: datetime | None,
        current_path: list[GraphEdge],
        all_paths: list[list[GraphEdge]],
        max_depth: int,
        visited: set[str],
    ) -> None:
        """Depth-first search for attribution paths."""
        if len(current_path) >= max_depth:
            return
        if node_id in visited:
            return

        visited.add(node_id)
        edges = self.get_edges_from(node_id, at_time)

        if not edges:
            # Leaf node: save path if non-empty.
            if current_path:
                all_paths.append(list(current_path))
            visited.discard(node_id)
            return

        for edge in edges:
            current_path.append(edge)
            # Check if we've reached a cost center (terminal node).
            target_node = self.nodes.get(edge.to_id)
            if target_node and target_node.node_type in (NodeType.COST_CENTER, NodeType.BUDGET):
                all_paths.append(list(current_path))
            else:
                self._dfs(edge.to_id, at_time, current_path, all_paths, max_depth, visited)
            current_path.pop()

        visited.discard(node_id)

    def get_stats(self) -> dict[str, int]:
        """Graph statistics."""
        active_edges = sum(1 for e in self.edges if e.valid_to is None)
        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "active_edges": active_edges,
            "historical_edges": len(self.edges) - active_edges,
        }
