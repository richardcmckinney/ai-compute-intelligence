"""
Evidence combination using modified noisy-OR belief combination.

Patent Spec Section 5.3: The system combines evidence from multiple
reconciliation methods using:

    C_combined = min(0.95, 1 - product(1 - C_i * w_i))

where C_i is calibrated confidence from method i, w_i is the dependency-adjusted
weight, and the result is capped at 0.95.

Three safeguards prevent pathological combination:
(a) Capping: combined confidence never exceeds 0.95.
(b) Diminishing returns: each additional signal adds less.
(c) Dependency penalty: correlated signals are discounted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aci.models.confidence import (
    DependencyPenalty,
    EvidenceCombinationResult,
    IndividualEvidence,
)

# Known signal correlations: method pairs that are likely correlated.
# These are initial defaults; the system estimates actual correlation from
# mutual information on historical data and adjusts per deployment.
DEFAULT_CORRELATED_PAIRS: dict[tuple[str, str], float] = {
    ("R3", "R4"): 0.5,  # Naming convention + historical pattern both derive from naming.
    ("R2", "R5"): 0.4,  # Temporal + service account share temporal signals.
    ("R4", "R5"): 0.3,  # Historical + service account share deployment history.
}


@dataclass
class CombinationConfig:
    """Configuration for evidence combination."""

    cap: float = 0.95
    diminishing_factor: float = 0.5
    default_dependency_discount: float = 0.5
    correlated_pairs: dict[tuple[str, str], float] | None = None

    def __post_init__(self) -> None:
        if self.correlated_pairs is None:
            self.correlated_pairs = dict(DEFAULT_CORRELATED_PAIRS)


def combine_evidence(
    signals: list[tuple[str, float]],
    config: CombinationConfig | None = None,
    learned_correlations: dict[tuple[str, str], float] | None = None,
) -> EvidenceCombinationResult:
    """
    Combine multiple reconciliation signals into a single confidence score.

    This is the core evidence combination function described in Section 5.3.
    It implements the modified noisy-OR with dependency correction and ceiling constraint.

    Args:
        signals: List of (method_name, calibrated_confidence) tuples.
        config: Combination parameters. Uses defaults if None.
        learned_correlations: Mutual-information-derived correlation estimates
            that override defaults. Updated as ground truth accumulates.

    Returns:
        EvidenceCombinationResult with combined confidence and full decomposition.
    """
    if config is None:
        config = CombinationConfig()

    bounded_signals = [(method, max(0.0, min(1.0, conf))) for method, conf in signals]

    if not bounded_signals:
        return EvidenceCombinationResult(
            combined_confidence=0.0,
            individual_confidences=[],
        )

    # Single signal: no combination needed, still apply cap.
    if len(bounded_signals) == 1:
        method, conf = bounded_signals[0]
        capped_conf = min(config.cap, conf)
        return EvidenceCombinationResult(
            combined_confidence=capped_conf,
            individual_confidences=[
                IndividualEvidence(
                    method=method,
                    raw_confidence=conf,
                    calibrated_confidence=conf,
                    weight=1.0,
                ),
            ],
            capped=capped_conf < conf,
        )

    correlations = config.correlated_pairs or {}
    if learned_correlations:
        correlations = {**correlations, **learned_correlations}

    # Step 1: Compute dependency-adjusted weights.
    individual: list[IndividualEvidence] = []
    penalties: list[DependencyPenalty] = []
    weights: dict[str, float] = {}

    for method, _confidence in bounded_signals:
        weights[method] = 1.0  # Start fully independent.

    for i, (method_a, _) in enumerate(bounded_signals):
        for j, (method_b, _) in enumerate(bounded_signals):
            if j <= i:
                continue

            pair = (method_a, method_b)
            pair_rev = (method_b, method_a)

            discount = correlations.get(pair) or correlations.get(pair_rev)
            if discount is not None:
                # Apply dependency discount to the weaker signal.
                if bounded_signals[i][1] <= bounded_signals[j][1]:
                    weights[method_a] = min(weights[method_a], discount)
                else:
                    weights[method_b] = min(weights[method_b], discount)

                penalties.append(
                    DependencyPenalty(
                        method_a=method_a,
                        method_b=method_b,
                        discount_factor=discount,
                        rationale="correlated_signals",
                    )
                )

    # Step 2: Apply diminishing returns (from 3rd signal onward).
    # Two independent signals combine at full weight. The diminishing
    # returns safeguard prevents accumulation of many weak signals to
    # false certainty, but does not penalize the common two-signal case.
    # Dependency penalties (Step 1) handle correlated pairs regardless
    # of position, preventing double-penalization.
    sorted_signals = sorted(bounded_signals, key=lambda x: x[1], reverse=True)
    diminishing_multipliers: list[float] = []
    for idx in range(len(sorted_signals)):
        if idx < 2:
            # First two signals: full weight (subject to dependency discount).
            diminishing_multipliers.append(1.0)
        else:
            # Signal 3+: each subsequent adds less.
            prev = diminishing_multipliers[idx - 1]
            diminishing_multipliers.append(prev * config.diminishing_factor)

    # Step 3: Modified noisy-OR combination.
    # C_combined = min(0.95, 1 - product(1 - C_i * w_i * d_i))
    product_term = 1.0
    for idx, (method, conf) in enumerate(sorted_signals):
        w = weights.get(method, 1.0)
        d = diminishing_multipliers[idx]
        effective_conf = min(1.0, max(0.0, conf * w * d))
        product_term *= 1.0 - effective_conf

        individual.append(
            IndividualEvidence(
                method=method,
                raw_confidence=conf,
                calibrated_confidence=conf,
                weight=round(w * d, 4),
            )
        )

    combined = 1.0 - product_term
    capped = combined > config.cap
    combined = min(config.cap, combined)

    return EvidenceCombinationResult(
        combined_confidence=round(combined, 4),
        individual_confidences=individual,
        dependency_penalties_applied=penalties,
        capped=capped,
        diminishing_returns_applied=len(bounded_signals) > 2,
    )


def combine_noisy_or_simple(confidences: list[float], cap: float = 0.95) -> float:
    """
    Simplified noisy-OR for cases where all signals are independent.

    C = min(cap, 1 - product(1 - c_i))
    """
    if not confidences:
        return 0.0
    product_term = math.prod(1.0 - c for c in confidences)
    return min(cap, 1.0 - product_term)
