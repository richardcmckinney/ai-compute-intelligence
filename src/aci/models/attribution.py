"""
Attribution index entry: the precomputed, materialized view served at decision time.

The attribution index is NOT a cache of raw source data (Patent Spec Section 3.1).
It is a synthesized control artifact derived from probabilistic reconciliation,
incorporating fractional attribution, confidence calibration, and conflict resolution.
Its structure is intentionally constrained to support deterministic, low-latency,
constant-time O(1) decision-time enforcement.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class AttributionIndexEntry(BaseModel):
    """
    A single entry in the precomputed attribution index.

    Keyed by workload identifier (service name, API key, or resource ARN).
    All fields required for decision-time enforcement are pre-resolved
    so the interceptor performs a single hash lookup with no graph traversal.
    """

    # Lookup key: the identifier the interceptor uses to find this entry.
    workload_id: str = Field(description="Service name, API key, or resource ARN")

    # Pre-resolved attribution chain.
    team_id: str
    team_name: str
    cost_center_id: str
    cost_center_name: str = ""
    person_id: str = ""
    person_name: str = ""
    repository: str = ""
    service_name: str = ""

    # Confidence governance.
    confidence: float = Field(ge=0.0, le=1.0, description="Calibrated attribution confidence")
    confidence_tier: str = Field(description="chargeback_ready | provisional | estimated")
    method_used: str = Field(description="Primary reconciliation method (R1-R6)")

    # Execution constraints (pre-evaluated policies).
    budget_remaining_usd: float | None = None
    budget_limit_usd: float | None = None
    model_allowlist: list[str] = Field(default_factory=list)
    cost_ceiling_per_request_usd: float | None = None
    token_budget_output: int | None = None
    token_budget_input: int | None = None

    # Equivalence class (for model routing decisions).
    equivalence_class_id: str | None = None
    approved_alternatives: list[str] = Field(default_factory=list)

    # Carbon context.
    carbon_methodology_layer: int = 2
    grid_region: str = ""

    # Index metadata.
    version: int = Field(default=1, description="Index entry version for cache coherence")
    materialized_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_event_ids: list[str] = Field(
        default_factory=list,
        description="Event IDs that contributed to this materialization",
    )
    time_bucket: str = Field(
        default="",
        description="Discrete time bucket for deterministic reconstruction",
    )


class AttributionResult(BaseModel):
    """
    Complete attribution result from graph traversal.

    This is the full-fidelity result before materialization into the compact
    index entry. Contains the full path, fractional weights, and explanation.
    """

    workload_id: str
    attribution_path: list[AttributionPathNode]
    combined_confidence: float
    explanation: ExplanationArtifact | None = None
    fractional_attributions: list[FractionalAttribution] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list, description="Conflict state IDs")


class AttributionPathNode(BaseModel):
    """A single node in the attribution path from inference to cost center."""

    layer: str = Field(description="Model | Service | Code | Identity | Org | Budget")
    node_id: str
    node_label: str
    confidence: float
    method: str = Field(description="Reconciliation method that produced this link")
    source: str = Field(description="Data source system")


class FractionalAttribution(BaseModel):
    """
    Fractional cost split when a workload is shared across teams.

    Section 4.2: Edge weights (0..1) enable this natively. Traversal treats
    cost as a 'fluid' splitting across weighted edges.
    """

    team_id: str
    team_name: str
    cost_center_id: str
    weight: float = Field(ge=0.0, le=1.0, description="Fraction of cost attributed")
    confidence: float


class ExplanationArtifact(BaseModel):
    """
    Explanation bundle for probabilistic attributions (Section 5.4).

    Stored for every probabilistic attribution. Surfaced in dashboards and
    API responses. Critical for enterprise trust and §101 defense (system
    produces concrete, auditable technical artifacts).
    """

    attribution_id: str
    target_entity: str
    confidence_score: float
    method_used: str
    top_contributing_signals: list[dict[str, Any]] = Field(default_factory=list)
    feature_values: dict[str, Any] = Field(
        default_factory=dict,
        description="e.g., {time_delta: 5, string_similarity: 0.72, prior_match_count: 14}",
    )
    alternatives_considered: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Other possible attributions with their confidence scores",
    )
    conflict_state: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Ensure forward references are resolved deterministically for runtime validators.
AttributionResult.model_rebuild()
