"""Intervention lifecycle registry for recommendation state management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Literal

InterventionStatus = Literal[
    "recommended",
    "review",
    "approved",
    "implemented",
    "dismissed",
]


@dataclass(slots=True)
class InterventionRecord:
    """Single optimization recommendation with lifecycle metadata."""

    intervention_id: str
    title: str
    intervention_type: str
    team: str
    detail: str
    savings_usd_month: float
    confidence_pct: float
    risk: str
    equivalence_mode: str
    equivalence_status: str
    category: str
    threshold_condition: str
    rule_condition: str
    status: InterventionStatus = "recommended"
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_by: str = "system"
    update_note: str = ""


@dataclass(slots=True)
class InterventionSummary:
    """Aggregated intervention metrics used for dashboards."""

    total_count: int
    recommended_count: int
    review_count: int
    approved_count: int
    implemented_count: int
    dismissed_count: int
    total_potential_usd: float
    captured_savings_usd: float
    open_potential_usd: float


class InterventionRegistry:
    """In-memory registry with bounded, explicit transition semantics."""

    _ALLOWED_STATUS: tuple[InterventionStatus, ...] = (
        "recommended",
        "review",
        "approved",
        "implemented",
        "dismissed",
    )

    def __init__(self, interventions: list[InterventionRecord] | None = None) -> None:
        self._lock = Lock()
        self._records: dict[str, InterventionRecord] = {
            record.intervention_id: record for record in interventions or []
        }

    @classmethod
    def with_seed_data(cls) -> InterventionRegistry:
        """Bootstrap deterministic records for local runtime and demos."""
        seeded = [
            InterventionRecord(
                intervention_id="INT-401",
                title="Route fallback traffic from gpt-4o to gpt-4o-mini",
                intervention_type="Model Routing",
                team="Product",
                detail=(
                    "2.1M fallback requests analyzed. "
                    "97.3% quality parity in Mode 2a shadow evaluation."
                ),
                savings_usd_month=31_200.0,
                confidence_pct=94.0,
                risk="low",
                equivalence_mode="Empirical (Mode 2a)",
                equivalence_status="verified",
                category="Model over-provisioning",
                threshold_condition="2.1M monthly fallback requests and 72% cost differential",
                rule_condition="Cost differential > 50% and equivalence confidence >= 0.93",
                status="recommended",
            ),
            InterventionRecord(
                intervention_id="INT-402",
                title="Apply input-token guardrail to analytics-batch",
                intervention_type="Token Governance",
                team="ML Engineering",
                detail=(
                    "95th percentile prompt size is 4.2K. "
                    "Capping at 3.5K removes outlier spend."
                ),
                savings_usd_month=14_800.0,
                confidence_pct=89.0,
                risk="medium",
                equivalence_mode="Policy floor",
                equivalence_status="conditional",
                category="Prompt inefficiency",
                threshold_condition="p95 input tokens exceed floor by >20%",
                rule_condition="Recommend compaction when p95 > 4K",
                status="review",
            ),
            InterventionRecord(
                intervention_id="INT-403",
                title="Enable semantic cache for deterministic support prompts",
                intervention_type="Semantic Cache",
                team="Customer Engineering",
                detail="Cache hit rate stabilized at 38% with no measurable quality drop.",
                savings_usd_month=10_200.0,
                confidence_pct=92.0,
                risk="low",
                equivalence_mode="Empirical (Mode 2a)",
                equivalence_status="verified",
                category="Duplicate requests",
                threshold_condition="Semantic duplicate ratio > 30%",
                rule_condition="Enable cache when duplicate ratio >= 0.30",
                status="implemented",
            ),
            InterventionRecord(
                intervention_id="INT-404",
                title="Restrict staging model allowlist to low-cost equivalents",
                intervention_type="Policy Enforcement",
                team="Platform Engineering",
                detail="Staging traffic uses production-tier models without quality benefit.",
                savings_usd_month=22_800.0,
                confidence_pct=87.0,
                risk="medium",
                equivalence_mode="Policy (Mode 1)",
                equivalence_status="policy",
                category="Staging and dev sprawl",
                threshold_condition=">25% staging requests use frontier-tier models",
                rule_condition="Apply staging allowlist when non-prod frontier usage > 20%",
                status="recommended",
            ),
        ]
        return cls(interventions=seeded)

    def list_records(self, status: str | None = None) -> list[InterventionRecord]:
        """Return records sorted by monthly savings descending."""
        with self._lock:
            records = list(self._records.values())

        if status is not None:
            self._validate_status(status)
            records = [record for record in records if record.status == status]

        return sorted(records, key=lambda record: record.savings_usd_month, reverse=True)

    def summary(self) -> InterventionSummary:
        """Return aggregate lifecycle counts and dollar impact metrics."""
        records = self.list_records()

        recommended_count = sum(record.status == "recommended" for record in records)
        review_count = sum(record.status == "review" for record in records)
        approved_count = sum(record.status == "approved" for record in records)
        implemented_count = sum(record.status == "implemented" for record in records)
        dismissed_count = sum(record.status == "dismissed" for record in records)

        total_potential_usd = sum(record.savings_usd_month for record in records)
        captured_savings_usd = sum(
            record.savings_usd_month
            for record in records
            if record.status in {"approved", "implemented"}
        )
        open_potential_usd = sum(
            record.savings_usd_month
            for record in records
            if record.status in {"recommended", "review"}
        )

        return InterventionSummary(
            total_count=len(records),
            recommended_count=recommended_count,
            review_count=review_count,
            approved_count=approved_count,
            implemented_count=implemented_count,
            dismissed_count=dismissed_count,
            total_potential_usd=total_potential_usd,
            captured_savings_usd=captured_savings_usd,
            open_potential_usd=open_potential_usd,
        )

    def transition(
        self,
        intervention_id: str,
        next_status: InterventionStatus,
        *,
        actor: str = "api",
        note: str = "",
    ) -> InterventionRecord:
        """Transition recommendation to a new lifecycle state."""
        self._validate_status(next_status)

        with self._lock:
            record = self._records.get(intervention_id)
            if record is None:
                raise KeyError(f"Unknown intervention_id: {intervention_id}")

            record.status = next_status
            record.updated_at = datetime.now(UTC)
            record.updated_by = actor
            record.update_note = note

            return record

    def _validate_status(self, status: str) -> None:
        if status not in self._ALLOWED_STATUS:
            allowed = ", ".join(self._ALLOWED_STATUS)
            raise ValueError(f"Invalid intervention status '{status}'. Allowed: {allowed}")
