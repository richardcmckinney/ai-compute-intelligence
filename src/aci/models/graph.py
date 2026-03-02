"""
Attribution graph node and edge types.

The graph is a two-tier architecture (Patent Spec Section 3.1):
- Tier 1 (Authoritative Graph Store): Full property graph with time-versioned edges,
  stored in Neo4j. Updated asynchronously by the reconciliation engine.
- Tier 2 (Attribution Index): Precomputed, materialized view for constant-time
  decision-time lookups. Stored in Redis / in-memory.

All edges carry valid_from and valid_to timestamps (Section 4.2). The graph represents
attribution as it was at any historical point, not just as it is now.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    """
    Graph node types (Section 4.1).

    Original 7 from v1.0 plus 5 additions for enterprise complexity.
    """

    # Core v1.0 types.
    MODEL = "model"
    CLOUD_RESOURCE = "cloud_resource"
    DEPLOYMENT = "deployment"
    REPOSITORY = "repository"
    PERSON = "person"
    TEAM = "team"
    COST_CENTER = "cost_center"

    # Extended v2.0 types for real enterprise environments.
    SERVICE_ACCOUNT = "service_account"     # Shared accounts that break identity resolution.
    API_KEY = "api_key"                     # API keys that mediate access to models.
    INFERENCE_ENDPOINT = "inference_endpoint"  # Logical endpoint aggregating cloud resources.
    EQUIVALENCE_CLASS = "equivalence_class"   # Model groupings for capability equivalence.
    BUDGET = "budget"                       # GL codes and budget allocations.


class EdgeType(StrEnum):
    """Relationship types between graph nodes."""

    INVOKES = "invokes"               # Service -> Model
    DEPLOYED_ON = "deployed_on"       # Service -> CloudResource
    DEPLOYED_BY = "deployed_by"       # Deployment -> Person or ServiceAccount
    OWNS_CODE = "owns_code"           # Person/Team -> Repository
    MEMBER_OF = "member_of"           # Person -> Team
    REPORTS_TO = "reports_to"         # Team -> CostCenter (or Person -> Person)
    AUTHENTICATES = "authenticates"   # APIKey -> Person or ServiceAccount
    RESOLVED_TO = "resolved_to"      # ServiceAccount -> Person (probabilistic)
    ATTRIBUTED_TO = "attributed_to"  # InferenceEndpoint -> Team
    BUDGETED_UNDER = "budgeted_under"  # Team -> Budget / CostCenter
    CLASSIFIED_AS = "classified_as"  # Model -> EquivalenceClass
    TRIGGERS = "triggers"            # Deployment -> CloudResource


class GraphNode(BaseModel):
    """
    A node in the attribution graph.

    Nodes are typed entities with a globally unique identifier, a human-readable
    label, and arbitrary properties for enrichment.
    """

    node_id: str = Field(description="Globally unique identifier")
    node_type: NodeType
    label: str = Field(description="Human-readable display name")
    properties: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GraphEdge(BaseModel):
    """
    A time-versioned, weighted edge in the attribution graph.

    Critical v2.0 change (Section 4.2): all edges carry valid_from and valid_to
    timestamps. People change teams. Repos change owners. The graph must represent
    attribution as it was at any historical point.

    Edge weights (0..1) enable fractional attribution. A shared inference endpoint
    serving 3 workloads splits cost proportionally. Traversal treats cost as a
    "fluid" splitting across weighted edges.
    """

    edge_type: EdgeType
    from_id: str = Field(description="Source node ID")
    to_id: str = Field(description="Target node ID")
    confidence: float = Field(ge=0.0, le=1.0, description="Calibrated confidence score")
    weight: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Fractional attribution weight (for split attribution)",
    )
    valid_from: datetime = Field(description="Start of validity window")
    valid_to: datetime | None = Field(
        default=None,
        description="End of validity window (None = currently valid)",
    )
    provenance: EdgeProvenance = Field(description="How this edge was derived")
    explanation_ref: str | None = Field(
        default=None,
        description="Reference to explanation artifact for probabilistic edges",
    )


class EdgeProvenance(BaseModel):
    """
    Provenance record for how an edge was derived.

    Every probabilistic edge stores: source system, reconciliation method,
    and the contributing signals. This supports auditability and the
    calibration loop (Section 5.2).
    """

    source: str = Field(description="System that produced this edge (e.g., 'hre', 'manual')")
    method: str = Field(description="Reconciliation method (R1-R6 or 'deterministic')")
    signals: list[str] = Field(
        default_factory=list,
        description="Contributing signal identifiers",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConflictState(BaseModel):
    """
    Persisted conflict state when two deterministic paths disagree.

    Section 4.2: Conflicts are persisted for audit rather than silently resolved.
    Resolution uses a precedence table: CI/CD deploy trigger > repo ownership > HR hierarchy.
    """

    node_id: str
    conflicting_edges: list[str] = Field(description="Edge IDs in conflict")
    resolution: str | None = Field(default=None, description="Chosen resolution")
    resolution_method: str = Field(
        default="unresolved",
        description="How conflict was resolved: precedence_table | manual | unresolved",
    )
    precedence_applied: list[str] = Field(
        default_factory=list,
        description="Ordered list of precedence rules evaluated",
    )
    resolved_at: datetime | None = None
