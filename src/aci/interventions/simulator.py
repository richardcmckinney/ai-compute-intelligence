"""Pre-deploy cost simulation for right-sizing recommendations."""

from __future__ import annotations

from dataclasses import dataclass

from aci.pricing.catalog import PricingCatalog, PricingUsage


@dataclass(frozen=True)
class CandidateSimulation:
    """One candidate model simulation result."""

    model: str
    projected_monthly_cost_usd: float
    monthly_savings_usd: float
    savings_pct: float


@dataclass(frozen=True)
class CostSimulationResult:
    """Overall simulation envelope for a service."""

    service_id: str
    provider: str
    current_model: str
    projected_monthly_cost_usd: float
    candidates: list[CandidateSimulation]


class CostSimulationEngine:
    """Estimate monthly costs for baseline and candidate routing choices."""

    def __init__(self, pricing: PricingCatalog) -> None:
        self.pricing = pricing

    def simulate(
        self,
        *,
        service_id: str,
        provider: str,
        current_model: str,
        avg_input_tokens: int,
        avg_output_tokens: int,
        requests_per_day: int,
        candidate_models: list[str],
    ) -> CostSimulationResult:
        if requests_per_day <= 0:
            msg = "requests_per_day must be > 0"
            raise ValueError(msg)

        monthly_requests = requests_per_day * 30
        baseline = self.pricing.estimate(
            PricingUsage(
                provider=provider,
                model=current_model,
                input_tokens=avg_input_tokens,
                output_tokens=avg_output_tokens,
                request_count=monthly_requests,
            )
        )

        candidates: list[CandidateSimulation] = []
        for model in candidate_models:
            if model == current_model:
                continue
            estimate = self.pricing.estimate(
                PricingUsage(
                    provider=provider,
                    model=model,
                    input_tokens=avg_input_tokens,
                    output_tokens=avg_output_tokens,
                    request_count=monthly_requests,
                )
            )
            savings = baseline.total_cost_usd - estimate.total_cost_usd
            savings_pct = 0.0
            if baseline.total_cost_usd > 0:
                savings_pct = (savings / baseline.total_cost_usd) * 100.0
            candidates.append(
                CandidateSimulation(
                    model=model,
                    projected_monthly_cost_usd=estimate.total_cost_usd,
                    monthly_savings_usd=savings,
                    savings_pct=savings_pct,
                )
            )

        candidates.sort(key=lambda candidate: candidate.monthly_savings_usd, reverse=True)
        return CostSimulationResult(
            service_id=service_id,
            provider=provider,
            current_model=current_model,
            projected_monthly_cost_usd=baseline.total_cost_usd,
            candidates=candidates,
        )
