"""
Confidence governance models (Patent Spec Section 5).

Confidence scores use an empirical calibration interpretation: a score of 0.8
means that among all attributions historically assigned 0.8, approximately 80%
were correct when validated against ground truth.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class ConfidenceTier(StrEnum):
    """Operational confidence thresholds (Section 5.1)."""

    CHARGEBACK_READY = "chargeback_ready"  # >= 0.80
    PROVISIONAL = "provisional"             # 0.50 - 0.79
    ESTIMATED = "estimated"                 # < 0.50


class CalibrationCurve(BaseModel):
    """
    Per-method reliability curve from isotonic regression (Section 5.2).

    Each reconciliation method (R1-R6) has its own calibration curve
    updated weekly from accumulated ground truth.
    """

    method: str = Field(description="Reconciliation method identifier (R1-R6)")
    raw_scores: list[float] = Field(description="Raw confidence outputs")
    calibrated_scores: list[float] = Field(description="Calibrated probabilities")
    sample_count: int = Field(description="Number of ground truth samples used")
    is_warm_start: bool = Field(
        default=True,
        description="True if using pre-calibrated curves, False if customer-specific",
    )
    ks_statistic: float | None = Field(
        default=None,
        description="KS distance from warm-start curve (triggers transition at > 0.15)",
    )
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    uncertainty_band: tuple[float, float] | None = Field(
        default=None,
        description="Bootstrap confidence interval when sample_count < 200",
    )


class GroundTruthLabel(BaseModel):
    """
    A ground truth label for calibration (Section 5.2).

    Sources: CI/CD provenance, explicit user corrections, FinOps review
    decisions, ticket-based approvals.
    """

    attribution_id: str
    true_team_id: str
    true_cost_center_id: str
    source: str = Field(description="cicd_provenance | user_correction | finops_review | ticket")
    predicted_team_id: str
    predicted_confidence: float
    method_used: str
    was_correct: bool
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceCombinationResult(BaseModel):
    """
    Result of combining multiple reconciliation signals (Section 5.3).

    Uses modified noisy-OR belief combination:
    C_combined = min(0.95, 1 - product(1 - C_i * w_i))
    """

    combined_confidence: float
    individual_confidences: list[IndividualEvidence]
    dependency_penalties_applied: list[DependencyPenalty] = Field(default_factory=list)
    capped: bool = Field(default=False, description="True if result hit the 0.95 cap")
    diminishing_returns_applied: bool = False


class IndividualEvidence(BaseModel):
    """A single reconciliation signal contributing to the combination."""

    method: str
    raw_confidence: float
    calibrated_confidence: float
    weight: float = Field(
        default=1.0,
        description="Dependency-adjusted weight (1.0 = independent, <1.0 = discounted)",
    )


class DependencyPenalty(BaseModel):
    """Record of a dependency penalty applied between two correlated signals."""

    method_a: str
    method_b: str
    discount_factor: float
    mutual_information_estimate: float | None = None
    rationale: str = ""
