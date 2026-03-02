"""
Carbon ledger models (Patent Spec Section 8) and TRAC (Section 7).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Carbon Ledger (Section 8)
# ---------------------------------------------------------------------------

class CarbonMethodologyLayer(StrEnum):
    BASELINE = "baseline"   # Layer 1: spend-based, +/- 50-100%
    STANDARD = "standard"   # Layer 2: activity-based, +/- 15-30%
    PREMIUM = "premium"     # Layer 3: instrumented, +/- 5-15%


class AccountingMethod(StrEnum):
    LOCATION_BASED = "location_based"
    MARKET_BASED = "market_based"


class GHGScope(StrEnum):
    SCOPE_2 = "scope_2"   # Direct cloud compute.
    SCOPE_3 = "scope_3"   # Third-party API inference (Cat. 1 purchased services).


class CarbonCalculationReceipt(BaseModel):
    """
    Immutable calculation receipt (Section 8.2).

    Every carbon figure carries full provenance metadata for compliance
    exports and third-party audit.
    """

    receipt_id: str
    workload_id: str
    emissions_kg_co2e: float
    uncertainty_band_pct: tuple[float, float] = Field(
        description="(lower_pct, upper_pct), e.g. (15.0, 30.0) for Layer 2",
    )

    # Methodology provenance.
    method_layer: CarbonMethodologyLayer
    accounting_method: AccountingMethod
    ghg_scope: GHGScope
    ghg_category: str = Field(default="", description="e.g., 'Category 1' for Scope 3")

    # Data source provenance.
    grid_factor_source: str = Field(description="e.g., 'EMBER 2025Q4', 'EPA eGRID 2024'")
    grid_factor_version: str = ""
    grid_factor_timestamp: datetime | None = None
    energy_source: str = Field(description="benchmark | estimated | measured")
    hardware_profile_id: str = ""
    embodied_carbon_assumptions: str = ""
    region_to_grid_mapping_version: str = ""

    # For Scope 3 third-party API calls (Section 8.3).
    provider_name: str = ""
    provider_emission_factor: float | None = Field(
        default=None, description="gCO2e per token for third-party API",
    )
    provider_factor_source: str = ""  # published_report | benchmark | estimated

    # Immutability metadata.
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    superseded_by: str | None = Field(
        default=None, description="Receipt ID of superseding calculation",
    )

    model_config = {"frozen": True}


class CarbonLedgerEntry(BaseModel):
    """A single entry in the carbon ledger tying emissions to attribution."""

    entry_id: str
    workload_id: str
    team_id: str
    cost_center_id: str
    attribution_confidence: float

    emissions_kg_co2e: float
    carbon_liability_usd: float = Field(
        description="emissions * internal_carbon_price / 1000",
    )

    receipt: CarbonCalculationReceipt
    period_start: datetime
    period_end: datetime


# ---------------------------------------------------------------------------
# TRAC Metric (Section 7)
# ---------------------------------------------------------------------------

class TRACResult(BaseModel):
    """
    Total Risk-Adjusted Cost computation result.

    TRAC(workload) = Billed Cost + Carbon Liability + Confidence Risk Premium

    Near-term value: pricing data-quality risk.
    Carbon is an embedded accounting line item that scales with regulatory exposure.
    """

    workload_id: str
    billed_cost_usd: float
    carbon_liability_usd: float
    confidence_risk_premium_usd: float
    trac_usd: float = Field(description="Sum of all three components")

    # Decomposition.
    attribution_confidence: float
    emissions_kg_co2e: float
    carbon_price_per_tco2e: float
    risk_multiplier: float

    # Sensitivity context (Section 7).
    carbon_pct_of_trac: float = Field(description="Carbon as % of TRAC")
    risk_pct_of_trac: float = Field(description="Confidence risk as % of TRAC")

    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Policy Models (Section 6.2, 6.4)
# ---------------------------------------------------------------------------

class PolicyType(StrEnum):
    MODEL_ALLOWLIST = "model_allowlist"
    BUDGET_CEILING = "budget_ceiling"
    TOKEN_BUDGET = "token_budget"
    COST_CEILING = "cost_ceiling"
    APPROVAL_GATE = "approval_gate"
    SHADOW_DETECTION = "shadow_detection"
    CONFIDENCE_FLOOR = "confidence_floor"


class EnforcementAction(StrEnum):
    ALLOW = "allow"
    SOFT_STOP = "soft_stop"      # Alert + log, request proceeds.
    HARD_STOP = "hard_stop"      # Block until approval.
    REDIRECT = "redirect"         # Route to alternative model.


class PolicyDefinition(BaseModel):
    """A governance policy evaluated at decision time."""

    policy_id: str
    policy_type: PolicyType
    name: str
    scope: str = Field(description="Scope expression: team, cloud, global, etc.")
    enforcement: EnforcementAction = Field(default=EnforcementAction.SOFT_STOP)
    parameters: dict = Field(default_factory=dict)
    enabled: bool = True
    requires_opt_in: bool = Field(
        default=False,
        description="True for hard stops; they are never applied by default.",
    )


class PolicyEvaluationResult(BaseModel):
    """Result of evaluating a policy against a request."""

    policy_id: str
    policy_name: str
    action: EnforcementAction
    violated: bool
    details: str = ""
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Capability Equivalence (Section 6.2)
# ---------------------------------------------------------------------------

class EquivalenceMode(StrEnum):
    POLICY = "policy"           # Mode 1: Admin-defined model families.
    EMPIRICAL = "empirical"     # Mode 2a: Shadow evaluation harness.
    JUDGE = "judge"             # Mode 2b: LLM-as-judge.
    CONTRACTUAL = "contractual"  # Mode 3: Compliance boundary.


class EquivalenceVerification(BaseModel):
    """Result of verifying model equivalence for a use case."""

    verification_id: str
    source_model: str
    candidate_model: str
    equivalence_class_id: str
    mode: EquivalenceMode
    is_equivalent: bool

    # Mode 2a/2b details.
    sample_count: int = 0
    baseline_score: float | None = None
    candidate_score: float | None = None
    quality_delta: float | None = None
    confidence_interval: tuple[float, float] | None = None

    # TTL management.
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    model_version_hash: str = ""

    # Fail-safe: if verification fails or is inconclusive, route to original.
    fail_safe_triggered: bool = False
