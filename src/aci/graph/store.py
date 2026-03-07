"""
Graph store implementations for the authoritative attribution graph.

The platform supports two runtime backends:
- InMemoryGraphStore: deterministic local/demo/testing backend.
- Neo4jGraphStore: durable production backend using the official Neo4j driver.

The interceptor never reads this store on the hot path. It reads the
materialized attribution index only.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast, overload

import structlog
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from aci.config import PlatformConfig, _secret_value
from aci.models.graph import EdgeProvenance, EdgeType, GraphEdge, GraphNode, NodeType

logger = structlog.get_logger()


class GraphStoreProtocol(Protocol):
    """Common contract for graph-store backends."""

    def start(self) -> None: ...

    def close(self) -> None: ...

    def ready(self) -> bool: ...

    def clear(self) -> None: ...

    def upsert_node(self, node: GraphNode) -> None: ...

    def add_edge(self, edge: GraphEdge) -> None: ...

    def close_edge(
        self,
        *,
        edge_type: EdgeType,
        from_id: str,
        to_id: str,
        valid_to: datetime | None = None,
    ) -> bool: ...

    def get_node(self, node_id: str) -> GraphNode | None: ...

    def get_nodes_by_type(self, node_type: NodeType) -> list[GraphNode]: ...

    def get_edges_from(self, node_id: str, at_time: datetime | None = None) -> list[GraphEdge]: ...

    def get_edges_to(self, node_id: str, at_time: datetime | None = None) -> list[GraphEdge]: ...

    def traverse_attribution(
        self,
        start_node_id: str,
        at_time: datetime | None = None,
        max_depth: int = 8,
        max_paths: int = 10_000,
    ) -> list[list[GraphEdge]]: ...

    def get_stats(self) -> dict[str, int]: ...


class _TraversalGraph(Protocol):
    def _dfs(
        self,
        node_id: str,
        at_time: datetime | None,
        current_path: list[GraphEdge],
        all_paths: list[list[GraphEdge]],
        max_depth: int,
        max_paths: int,
        visited: set[str],
    ) -> None: ...

    def get_edges_from(self, node_id: str, at_time: datetime | None = None) -> list[GraphEdge]: ...

    def get_node(self, node_id: str) -> GraphNode | None: ...


class _GraphTraversalMixin:
    """Shared traversal implementation across graph backends."""

    def traverse_attribution(
        self: _TraversalGraph,
        start_node_id: str,
        at_time: datetime | None = None,
        max_depth: int = 8,
        max_paths: int = 10_000,
    ) -> list[list[GraphEdge]]:
        paths: list[list[GraphEdge]] = []
        self._dfs(start_node_id, at_time, [], paths, max_depth, max_paths, set())
        return paths

    def _dfs(
        self: _TraversalGraph,
        node_id: str,
        at_time: datetime | None,
        current_path: list[GraphEdge],
        all_paths: list[list[GraphEdge]],
        max_depth: int,
        max_paths: int,
        visited: set[str],
    ) -> None:
        if len(all_paths) >= max_paths:
            return
        if len(current_path) >= max_depth:
            return
        if node_id in visited:
            return

        visited.add(node_id)
        edges = self.get_edges_from(node_id, at_time)

        if not edges:
            if current_path:
                all_paths.append(list(current_path))
            visited.discard(node_id)
            return

        for edge in edges:
            if len(all_paths) >= max_paths:
                break
            current_path.append(edge)
            target_node = self.get_node(edge.to_id)
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


class InMemoryGraphStore(_GraphTraversalMixin):
    """Deterministic in-memory graph store for development, demo mode, and tests."""

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self._nodes_by_type: dict[NodeType, set[str]] = defaultdict(set)
        self.edges: list[GraphEdge] = []
        self._from_index: dict[str, list[int]] = defaultdict(list)
        self._to_index: dict[str, list[int]] = defaultdict(list)
        self._active_edge_index: dict[tuple[str, str, EdgeType], int] = {}

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None

    def ready(self) -> bool:
        return True

    def clear(self) -> None:
        self.nodes.clear()
        self._nodes_by_type.clear()
        self.edges.clear()
        self._from_index.clear()
        self._to_index.clear()
        self._active_edge_index.clear()

    def upsert_node(self, node: GraphNode) -> None:
        existing = self.nodes.get(node.node_id)
        if existing:
            self._nodes_by_type[existing.node_type].discard(existing.node_id)
            node = node.model_copy(update={"updated_at": datetime.now(UTC)})
        self.nodes[node.node_id] = node
        self._nodes_by_type[node.node_type].add(node.node_id)

    def add_edge(self, edge: GraphEdge) -> None:
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

    def close_edge(
        self,
        *,
        edge_type: EdgeType,
        from_id: str,
        to_id: str,
        valid_to: datetime | None = None,
    ) -> bool:
        key = (from_id, to_id, edge_type)
        existing_idx = self._active_edge_index.get(key)
        if existing_idx is None:
            return False

        existing = self.edges[existing_idx]
        if existing.valid_to is not None:
            self._active_edge_index.pop(key, None)
            return False

        closed_at = valid_to or datetime.now(UTC)
        self.edges[existing_idx] = existing.model_copy(update={"valid_to": closed_at})
        self._active_edge_index.pop(key, None)
        return True

    def get_node(self, node_id: str) -> GraphNode | None:
        return self.nodes.get(node_id)

    def get_nodes_by_type(self, node_type: NodeType) -> list[GraphNode]:
        node_ids = self._nodes_by_type.get(node_type, set())
        return [self.nodes[node_id] for node_id in node_ids]

    def get_edges_from(
        self,
        node_id: str,
        at_time: datetime | None = None,
    ) -> list[GraphEdge]:
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
        at = at_time or datetime.now(UTC)
        results: list[GraphEdge] = []
        for idx in self._to_index.get(node_id, []):
            edge = self.edges[idx]
            if edge.valid_from <= at and (edge.valid_to is None or edge.valid_to > at):
                results.append(edge)
        return results

    def get_stats(self) -> dict[str, int]:
        active_edges = len(self._active_edge_index)
        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "active_edges": active_edges,
            "historical_edges": len(self.edges) - active_edges,
        }


class Neo4jGraphStore(_GraphTraversalMixin):
    """Durable graph-store backend backed by Neo4j."""

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        database: str | None = None,
    ) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._driver: Driver | None = None

    def start(self) -> None:
        if self._driver is not None:
            return
        self._driver = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
        self._driver.verify_connectivity()
        self._ensure_schema()

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def ready(self) -> bool:
        try:
            driver = self._require_driver()
            with driver.session(database=self._database) as session:
                session.run("RETURN 1 AS ok").single(strict=True)
            return True
        except (Neo4jError, ServiceUnavailable, RuntimeError):
            return False

    def clear(self) -> None:
        self._run_write("MATCH (n:ACIEntity) DETACH DELETE n")

    def upsert_node(self, node: GraphNode) -> None:
        self._run_write(
            """
            MERGE (n:ACIEntity {node_id: $node_id})
            ON CREATE SET
              n.created_at_iso = $created_at_iso
            SET
              n.node_type = $node_type,
              n.label = $label,
              n.properties_json = $properties_json,
              n.tenant_id = $tenant_id,
              n.updated_at_iso = $updated_at_iso
            """,
            node_id=node.node_id,
            node_type=node.node_type.value,
            label=node.label,
            properties_json=_json_dump(node.properties),
            tenant_id=node.tenant_id,
            created_at_iso=_to_iso(node.created_at),
            updated_at_iso=_to_iso(datetime.now(UTC)),
        )

    def add_edge(self, edge: GraphEdge) -> None:
        closed_at_iso = _to_iso(datetime.now(UTC))
        self._run_write(
            """
            MERGE (src:ACIEntity {node_id: $from_id})
            ON CREATE SET
              src.node_type = $from_node_type,
              src.label = $from_label,
              src.properties_json = '{}',
              src.tenant_id = '',
              src.created_at_iso = $created_at_iso,
              src.updated_at_iso = $created_at_iso
            MERGE (dst:ACIEntity {node_id: $to_id})
            ON CREATE SET
              dst.node_type = $to_node_type,
              dst.label = $to_label,
              dst.properties_json = '{}',
              dst.tenant_id = '',
              dst.created_at_iso = $created_at_iso,
              dst.updated_at_iso = $created_at_iso
            WITH src, dst
            OPTIONAL MATCH (src)-[current:ACI_EDGE {edge_type: $edge_type}]->(dst)
            WHERE current.valid_to_iso IS NULL OR current.valid_to_iso = ''
            SET current.valid_to_iso = $closed_at_iso
            CREATE (src)-[:ACI_EDGE {
              edge_type: $edge_type,
              confidence: $confidence,
              weight: $weight,
              valid_from_iso: $valid_from_iso,
              valid_to_iso: $valid_to_iso,
              provenance_json: $provenance_json,
              explanation_ref: $explanation_ref
            }]->(dst)
            """,
            from_id=edge.from_id,
            to_id=edge.to_id,
            edge_type=edge.edge_type.value,
            confidence=edge.confidence,
            weight=edge.weight,
            valid_from_iso=_to_iso(edge.valid_from),
            valid_to_iso=_to_iso(edge.valid_to) if edge.valid_to is not None else "",
            provenance_json=_json_dump(edge.provenance.model_dump(mode="json")),
            explanation_ref=edge.explanation_ref or "",
            closed_at_iso=closed_at_iso,
            created_at_iso=_to_iso(datetime.now(UTC)),
            from_node_type=_infer_node_type(edge.from_id).value,
            to_node_type=_infer_node_type(edge.to_id).value,
            from_label=_default_label(edge.from_id),
            to_label=_default_label(edge.to_id),
        )

    def close_edge(
        self,
        *,
        edge_type: EdgeType,
        from_id: str,
        to_id: str,
        valid_to: datetime | None = None,
    ) -> bool:
        record = self._run_write(
            """
            MATCH (src:ACIEntity {node_id: $from_id})
              -[rel:ACI_EDGE {edge_type: $edge_type}]->
              (dst:ACIEntity {node_id: $to_id})
            WHERE rel.valid_to_iso IS NULL OR rel.valid_to_iso = ''
            SET rel.valid_to_iso = $valid_to_iso
            RETURN count(rel) AS updated
            """,
            from_id=from_id,
            to_id=to_id,
            edge_type=edge_type.value,
            valid_to_iso=_to_iso(valid_to or datetime.now(UTC)),
            single=True,
        )
        return bool(record and _as_int(record.get("updated"), default=0) > 0)

    def get_node(self, node_id: str) -> GraphNode | None:
        record = self._run_read(
            """
            MATCH (n:ACIEntity {node_id: $node_id})
            RETURN
              n.node_id AS node_id,
              n.node_type AS node_type,
              n.label AS label,
              n.properties_json AS properties_json,
              n.tenant_id AS tenant_id,
              n.created_at_iso AS created_at_iso,
              n.updated_at_iso AS updated_at_iso
            """,
            node_id=node_id,
            single=True,
        )
        if record is None:
            return None
        return _deserialize_node(record)

    def get_nodes_by_type(self, node_type: NodeType) -> list[GraphNode]:
        records = self._run_read(
            """
            MATCH (n:ACIEntity {node_type: $node_type})
            RETURN
              n.node_id AS node_id,
              n.node_type AS node_type,
              n.label AS label,
              n.properties_json AS properties_json,
              n.tenant_id AS tenant_id,
              n.created_at_iso AS created_at_iso,
              n.updated_at_iso AS updated_at_iso
            ORDER BY n.node_id ASC
            """,
            node_type=node_type.value,
        )
        return [_deserialize_node(record) for record in records]

    def get_edges_from(
        self,
        node_id: str,
        at_time: datetime | None = None,
    ) -> list[GraphEdge]:
        at_iso = _to_iso(at_time or datetime.now(UTC))
        records = self._run_read(
            """
            MATCH (src:ACIEntity {node_id: $node_id})-[rel:ACI_EDGE]->(dst:ACIEntity)
            WHERE rel.valid_from_iso <= $at_iso
              AND (rel.valid_to_iso IS NULL OR rel.valid_to_iso = '' OR rel.valid_to_iso > $at_iso)
            RETURN
              rel.edge_type AS edge_type,
              src.node_id AS from_id,
              dst.node_id AS to_id,
              rel.confidence AS confidence,
              rel.weight AS weight,
              rel.valid_from_iso AS valid_from_iso,
              rel.valid_to_iso AS valid_to_iso,
              rel.provenance_json AS provenance_json,
              rel.explanation_ref AS explanation_ref
            ORDER BY rel.valid_from_iso ASC
            """,
            node_id=node_id,
            at_iso=at_iso,
        )
        return [_deserialize_edge(record) for record in records]

    def get_edges_to(
        self,
        node_id: str,
        at_time: datetime | None = None,
    ) -> list[GraphEdge]:
        at_iso = _to_iso(at_time or datetime.now(UTC))
        records = self._run_read(
            """
            MATCH (src:ACIEntity)-[rel:ACI_EDGE]->(dst:ACIEntity {node_id: $node_id})
            WHERE rel.valid_from_iso <= $at_iso
              AND (rel.valid_to_iso IS NULL OR rel.valid_to_iso = '' OR rel.valid_to_iso > $at_iso)
            RETURN
              rel.edge_type AS edge_type,
              src.node_id AS from_id,
              dst.node_id AS to_id,
              rel.confidence AS confidence,
              rel.weight AS weight,
              rel.valid_from_iso AS valid_from_iso,
              rel.valid_to_iso AS valid_to_iso,
              rel.provenance_json AS provenance_json,
              rel.explanation_ref AS explanation_ref
            ORDER BY rel.valid_from_iso ASC
            """,
            node_id=node_id,
            at_iso=at_iso,
        )
        return [_deserialize_edge(record) for record in records]

    def get_stats(self) -> dict[str, int]:
        node_record = self._run_read(
            "MATCH (n:ACIEntity) RETURN count(n) AS total_nodes",
            single=True,
        )
        edge_record = self._run_read(
            """
            MATCH ()-[rel:ACI_EDGE]->()
            RETURN
              count(rel) AS total_edges,
              sum(
                CASE WHEN rel.valid_to_iso IS NULL OR rel.valid_to_iso = ''
                  THEN 1
                  ELSE 0
                END
              ) AS active_edges
            """,
            single=True,
        )
        total_nodes = _as_int(node_record.get("total_nodes"), default=0) if node_record else 0
        total_edges = _as_int(edge_record.get("total_edges"), default=0) if edge_record else 0
        active_edges = _as_int(edge_record.get("active_edges"), default=0) if edge_record else 0
        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "active_edges": active_edges,
            "historical_edges": total_edges - active_edges,
        }

    def _ensure_schema(self) -> None:
        self._run_write(
            "CREATE CONSTRAINT aci_entity_node_id IF NOT EXISTS "
            "FOR (n:ACIEntity) REQUIRE n.node_id IS UNIQUE"
        )
        self._run_write(
            "CREATE INDEX aci_entity_node_type IF NOT EXISTS FOR (n:ACIEntity) ON (n.node_type)"
        )

    def _require_driver(self) -> Driver:
        if self._driver is None:
            self.start()
        if self._driver is None:
            raise RuntimeError("neo4j graph store driver is not initialized")
        return self._driver

    @overload
    def _run_read(
        self,
        query: str,
        single: Literal[True],
        **params: object,
    ) -> dict[str, object] | None: ...

    @overload
    def _run_read(
        self,
        query: str,
        single: Literal[False] = False,
        **params: object,
    ) -> list[dict[str, object]]: ...

    def _run_read(
        self,
        query: str,
        single: bool = False,
        **params: object,
    ) -> dict[str, object] | list[dict[str, object]] | None:
        driver = self._require_driver()
        with driver.session(database=self._database) as session:
            parameters = cast("dict[str, Any]", dict(params))
            result = session.run(query, parameters)
            if single:
                record = result.single()
                return dict(record) if record is not None else None
            return [dict(record) for record in result]

    @overload
    def _run_write(
        self,
        query: str,
        single: Literal[True],
        **params: object,
    ) -> dict[str, object] | None: ...

    @overload
    def _run_write(
        self,
        query: str,
        single: Literal[False] = False,
        **params: object,
    ) -> None: ...

    def _run_write(
        self,
        query: str,
        single: bool = False,
        **params: object,
    ) -> dict[str, object] | None:
        driver = self._require_driver()
        with driver.session(database=self._database) as session:
            parameters = cast("dict[str, Any]", dict(params))
            result = session.run(query, parameters)
            if single:
                record = result.single()
                return dict(record) if record is not None else None
            list(result)
            return None


GraphStore = InMemoryGraphStore


def build_graph_store(config: PlatformConfig) -> GraphStoreProtocol:
    """Select the authoritative graph backend from configuration."""
    backend = config.graph_backend.lower()
    if backend == "neo4j":
        return Neo4jGraphStore(
            uri=config.neo4j_uri,
            user=config.neo4j_user,
            password=_secret_value(config.neo4j_password),
        )
    return InMemoryGraphStore()


def _json_dump(value: object) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _deserialize_node(record: dict[str, object]) -> GraphNode:
    return GraphNode(
        node_id=str(record["node_id"]),
        node_type=NodeType(str(record["node_type"])),
        label=str(record.get("label") or record["node_id"]),
        properties=_json_load(record.get("properties_json")),
        tenant_id=str(record.get("tenant_id") or ""),
        created_at=_from_iso(str(record.get("created_at_iso") or "")) or datetime.now(UTC),
        updated_at=_from_iso(str(record.get("updated_at_iso") or "")) or datetime.now(UTC),
    )


def _deserialize_edge(record: dict[str, object]) -> GraphEdge:
    provenance_raw = record.get("provenance_json")
    provenance = _json_load(provenance_raw)
    return GraphEdge(
        edge_type=EdgeType(_as_str(record["edge_type"])),
        from_id=_as_str(record["from_id"]),
        to_id=_as_str(record["to_id"]),
        confidence=_as_float(record.get("confidence"), default=0.0),
        weight=_as_float(record.get("weight"), default=1.0),
        valid_from=(
            _from_iso(_as_str(record.get("valid_from_iso"), default=""))
            or datetime.now(UTC)
        ),
        valid_to=_from_iso(_as_str(record.get("valid_to_iso"), default="")),
        provenance=EdgeProvenance.model_validate(provenance),
        explanation_ref=(
            _as_str(record.get("explanation_ref"))
            if record.get("explanation_ref")
            else None
        ),
    )


def _json_load(raw: object) -> dict[str, object]:
    if raw in {None, ""}:
        return {}
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _as_float(value: object, *, default: float) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    return float(str(value))


def _as_int(value: object, *, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return int(str(value))


def _as_str(value: object, *, default: str = "") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _infer_node_type(node_id: str) -> NodeType:
    prefix_map = {
        "model:": NodeType.MODEL,
        "resource:": NodeType.CLOUD_RESOURCE,
        "deploy:": NodeType.DEPLOYMENT,
        "repo:": NodeType.REPOSITORY,
        "person:": NodeType.PERSON,
        "team:": NodeType.TEAM,
        "cc:": NodeType.COST_CENTER,
        "cost_center:": NodeType.COST_CENTER,
        "service_account:": NodeType.SERVICE_ACCOUNT,
        "api_key:": NodeType.API_KEY,
        "endpoint:": NodeType.INFERENCE_ENDPOINT,
        "budget:": NodeType.BUDGET,
        "equivalence:": NodeType.EQUIVALENCE_CLASS,
    }
    normalized = node_id.lower()
    for prefix, node_type in prefix_map.items():
        if normalized.startswith(prefix):
            return node_type
    return NodeType.INFERENCE_ENDPOINT


def _default_label(node_id: str) -> str:
    return node_id.split(":", 1)[-1] if ":" in node_id else node_id
