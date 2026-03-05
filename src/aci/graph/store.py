"""
Graph store: Neo4j-backed attribution graph (Patent Spec Section 3.1, Tier 1).

The authoritative graph store maintains the full property graph with time-versioned
edges. It is updated asynchronously by the reconciliation engine. The interceptor
NEVER reads from this store; it reads only from the attribution index (Tier 2).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

import structlog

from aci.models.graph import EdgeType, GraphEdge, GraphNode, NodeType

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
        self._nodes_by_type: dict[NodeType, set[str]] = defaultdict(set)
        self.edges: list[GraphEdge] = []
        self._from_index: dict[str, list[int]] = defaultdict(list)
        self._to_index: dict[str, list[int]] = defaultdict(list)
        self._active_edge_index: dict[tuple[str, str, EdgeType], int] = {}

    def upsert_node(self, node: GraphNode) -> None:
        """Insert or update a node."""
        existing = self.nodes.get(node.node_id)
        if existing:
            self._nodes_by_type[existing.node_type].discard(existing.node_id)
        if existing:
            node = node.model_copy(update={"updated_at": datetime.now(UTC)})
        self.nodes[node.node_id] = node
        self._nodes_by_type[node.node_type].add(node.node_id)

    def add_edge(self, edge: GraphEdge) -> None:
        """
        Add a time-versioned edge.

        Edges are append-only: when relationships change, the old edge
        gets a valid_to timestamp and a new edge is created.
        """
        key = (edge.from_id, edge.to_id, edge.edge_type)
        now = datetime.now(UTC)

        existing_idx = self._active_edge_index.get(key)
        if existing_idx is not None:
            existing = self.edges[existing_idx]
            if existing.valid_to is None:
                self.edges[existing_idx] = existing.model_copy(update={"valid_to": now})

        new_idx = len(self.edges)
        self.edges.append(edge)
        self._from_index[edge.from_id].append(new_idx)
        self._to_index[edge.to_id].append(new_idx)
        self._active_edge_index[key] = new_idx

    def get_node(self, node_id: str) -> GraphNode | None:
        return self.nodes.get(node_id)

    def get_nodes_by_type(self, node_type: NodeType) -> list[GraphNode]:
        """Return all nodes of a specific type."""
        node_ids = self._nodes_by_type.get(node_type, set())
        return [self.nodes[node_id] for node_id in node_ids]

    def get_edges_from(
        self,
        node_id: str,
        at_time: datetime | None = None,
    ) -> list[GraphEdge]:
        """
        Get all outgoing edges from a node, optionally at a point in time.

        Time-versioned query: returns only edges valid at the specified time.
        """
        at = at_time or datetime.now(UTC)
        results: list[GraphEdge] = []
        for idx in self._from_index.get(node_id, []):
            edge = self.edges[idx]
            if edge.valid_from <= at and (edge.valid_to is None or edge.valid_to > at):
                results.append(edge)
        return results

    def get_edges_to(
        self,
        node_id: str,
        at_time: datetime | None = None,
    ) -> list[GraphEdge]:
        """Get all incoming edges to a node, optionally at a point in time."""
        at = at_time or datetime.now(UTC)
        results: list[GraphEdge] = []
        for idx in self._to_index.get(node_id, []):
            edge = self.edges[idx]
            if edge.valid_from <= at and (edge.valid_to is None or edge.valid_to > at):
                results.append(edge)
        return results

    def traverse_attribution(
        self,
        start_node_id: str,
        at_time: datetime | None = None,
        max_depth: int = 8,
        max_paths: int = 10_000,
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
        self._dfs(start_node_id, at_time, [], paths, max_depth, max_paths, set())
        return paths

    def _dfs(
        self,
        node_id: str,
        at_time: datetime | None,
        current_path: list[GraphEdge],
        all_paths: list[list[GraphEdge]],
        max_depth: int,
        max_paths: int,
        visited: set[str],
    ) -> None:
        """Depth-first search for attribution paths."""
        if len(all_paths) >= max_paths:
            return
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
            if len(all_paths) >= max_paths:
                break
            current_path.append(edge)
            # Check if we've reached a cost center (terminal node).
            target_node = self.nodes.get(edge.to_id)
            if target_node and target_node.node_type in (NodeType.COST_CENTER, NodeType.BUDGET):
                all_paths.append(list(current_path))
            else:
                self._dfs(
                    edge.to_id,
                    at_time,
                    current_path,
                    all_paths,
                    max_depth,
                    max_paths,
                    visited,
                )
            current_path.pop()

        visited.discard(node_id)

    def get_stats(self) -> dict[str, int]:
        """Graph statistics."""
        active_edges = len(self._active_edge_index)
        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "active_edges": active_edges,
            "historical_edges": len(self.edges) - active_edges,
        }
