"""
Total Risk-Adjusted Cost (TRAC) calculator (Patent Spec Section 7).

TRAC(workload) = Billed Cost + Carbon Liability + Confidence Risk Premium

Near-term value: pricing data-quality risk. Carbon is an embedded accounting
line item that scales with future regulatory exposure. TRAC is dominated by
billed cost and confidence risk at current carbon prices.
"""

from __future__ import annotations

import math

from aci.config import ConfidenceConfig, TRACConfig
from aci.models.carbon import TRACResult


class TRACCalculator:
    """
    Computes TRAC for a given workload.

    Components:
    - Billed Cost: actual cloud spend from billing APIs.
    - Carbon Liability: emissions * internal_carbon_price / 1000.
    - Confidence Risk Premium: billed_cost * (1 - confidence) * risk_multiplier.
      Derivation: if misattribution leads to chargeback disputes averaging D%
      of workload spend, and confidence C predicts misattribution rate (1-C),
      then risk_premium = billed_cost * (1-C) * D.
    """

    def __init__(
        self,
        trac_config: TRACConfig | None = None,
        confidence_config: ConfidenceConfig | None = None,
    ) -> None:
        self.trac_config = trac_config or TRACConfig()
        self.confidence_config = confidence_config or ConfidenceConfig()

    def compute(
        self,
        workload_id: str,
        billed_cost_usd: float,
        emissions_kg_co2e: float,
        attribution_confidence: float,
        signal_age_days: float = 0.0,
    ) -> TRACResult:
        """
        Compute TRAC for a workload.

        Args:
            workload_id: Workload identifier.
            billed_cost_usd: Actual cloud spend attributed to this workload.
            emissions_kg_co2e: Estimated carbon emissions in kg CO2e.
            attribution_confidence: Calibrated attribution confidence (0-0.95).
            signal_age_days: Age of the oldest contributing signal, for temporal decay.
        """
        # Apply temporal decay if signals are stale (Section 7).
        effective_confidence = attribution_confidence
        if signal_age_days > self.confidence_config.decay_window_days:
            excess = signal_age_days - self.confidence_config.decay_window_days
            decay = math.exp(-self.confidence_config.decay_rate * excess)
            effective_confidence = attribution_confidence * decay

        # Component 1: Carbon Liability.
        # emissions_kg * price_per_tonne / 1000 (converting kg to tonnes).
        carbon_liability = emissions_kg_co2e * self.trac_config.carbon_price_per_tco2e / 1000.0

        # Component 2: Confidence Risk Premium.
        # billed_cost * (1 - confidence) * risk_multiplier.
        risk_premium = (
            billed_cost_usd
            * (1.0 - effective_confidence)
            * self.trac_config.risk_multiplier
        )

        # TRAC = sum of all components.
        trac = billed_cost_usd + carbon_liability + risk_premium

        # Sensitivity decomposition.
        carbon_pct = (carbon_liability / trac * 100) if trac > 0 else 0.0
        risk_pct = (risk_premium / trac * 100) if trac > 0 else 0.0

        return TRACResult(
            workload_id=workload_id,
            billed_cost_usd=round(billed_cost_usd, 4),
            carbon_liability_usd=round(carbon_liability, 4),
            confidence_risk_premium_usd=round(risk_premium, 4),
            trac_usd=round(trac, 4),
            attribution_confidence=round(effective_confidence, 4),
            emissions_kg_co2e=round(emissions_kg_co2e, 4),
            carbon_price_per_tco2e=self.trac_config.carbon_price_per_tco2e,
            risk_multiplier=self.trac_config.risk_multiplier,
            carbon_pct_of_trac=round(carbon_pct, 2),
            risk_pct_of_trac=round(risk_pct, 2),
        )

    def compute_batch(
        self,
        workloads: list[dict],
    ) -> list[TRACResult]:
        """Compute TRAC for multiple workloads."""
        return [
            self.compute(
                workload_id=w["workload_id"],
                billed_cost_usd=w["billed_cost_usd"],
                emissions_kg_co2e=w.get("emissions_kg_co2e", 0.0),
                attribution_confidence=w["attribution_confidence"],
                signal_age_days=w.get("signal_age_days", 0.0),
            )
            for w in workloads
        ]
