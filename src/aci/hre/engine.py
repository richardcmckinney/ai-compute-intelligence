"""
Heuristic Reconciliation Engine: main orchestrator.

Processes events, performs entity resolution across disconnected domains,
and produces attribution results with calibrated confidence (Section 4).

The reconciliation hierarchy is applied in priority order (R1 first).
When R1 produces a deterministic match, lower-priority methods are skipped.
When R1 fails, R2-R6 are attempted and combined via noisy-OR.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

import structlog

from aci.hre.combination import CombinationConfig, combine_evidence
from aci.hre.methods import (
    R1DirectMatch,
    R2TemporalCorrelation,
    R3NamingConvention,
    R4HistoricalPattern,
    R5ServiceAccountResolution,
    R6ProportionalAllocation,
    ReconciliationSignal,
)
from aci.models.attribution import (
    AttributionPathNode,
    AttributionResult,
    ExplanationArtifact,
    FractionalAttribution,
)

logger = structlog.get_logger()


@dataclass
class HREExecutionConfig:
    """Execution-time safeguards for production reconciliation throughput."""

    max_resolve_ms: float = 25.0
    max_cluster_targets: int = 25
    max_historical_signals: int = 500
    cache_ttl_seconds: float = 30.0
    cache_max_entries: int = 10_000


class ReconciliationContext:
    """
    Encapsulates all data sources available for reconciliation.

    In production, these are populated from the graph store and ingestion
    connectors. For testing, they can be injected directly.
    """

    def __init__(self) -> None:
        # R1: Direct identifier mappings (api_key -> person, arn -> service, etc.).
        self.identity_mappings: dict[str, str] = {}

        # R2: Recent events from other systems for temporal correlation.
        self.temporal_events: list[tuple[datetime, str, str]] = []

        # R3: Known naming patterns per team.
        self.naming_patterns: dict[str, list[str]] = {}

        # R4: Historical attributions for this entity.
        self.historical_attributions: list[tuple[str, float]] = []

        # R5: Service account resolution context.
        self.deployment_owners: list[tuple[str, datetime]] = []
        self.code_owners: list[str] = []
        self.recent_users: list[tuple[str, datetime]] = []

        # R6: Known proportional usage of shared resources.
        self.proportional_users: dict[str, float] = {}


class HeuristicReconciliationEngine:
    """
    Main HRE orchestrator (Section 4.3, 4.4).

    The engine attempts reconciliation methods in priority order:
    1. R1 (Direct Match) - if deterministic, stop.
    2. R2-R5 (Probabilistic) - collect all signals.
    3. R6 (Proportional Allocation) - fallback if no other method works.

    Signals from R2-R5 are combined via noisy-OR (Section 5.3).
    """

    def __init__(
        self,
        combination_config: CombinationConfig | None = None,
        execution_config: HREExecutionConfig | None = None,
    ) -> None:
        self.r1 = R1DirectMatch()
        self.r2 = R2TemporalCorrelation()
        self.r3 = R3NamingConvention()
        self.r4 = R4HistoricalPattern()
        self.r5 = R5ServiceAccountResolution()
        self.r6 = R6ProportionalAllocation()
        self.combination_config = combination_config or CombinationConfig()
        self.execution_config = execution_config or HREExecutionConfig()
        self._cache: OrderedDict[str, tuple[float, AttributionResult]] = OrderedDict()

    def resolve(
        self,
        entity_id: str,
        entity_type: str,
        event_time: datetime,
        context: ReconciliationContext,
    ) -> AttributionResult:
        """
        Resolve an entity to its organizational owner.

        This is the core attribution pipeline:
        1. Attempt R1 deterministic match.
        2. If R1 fails, collect signals from R2-R5.
        3. Combine signals via noisy-OR.
        4. If combined confidence is too low, fall back to R6.
        5. Produce an AttributionResult with explanation artifact.
        """
        started = time.monotonic()
        cache_key = f"{entity_type}:{entity_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        logger.info(
            "hre.resolve.start",
            entity_id=entity_id,
            entity_type=entity_type,
        )

        # Step 1: Attempt R1 (deterministic).
        r1_signal = self.r1.resolve(entity_id, context.identity_mappings)
        if r1_signal is not None and r1_signal.confidence >= 0.98:
            logger.info("hre.resolve.r1_match", target=r1_signal.target_entity)
            result = self._build_result(
                entity_id=entity_id,
                primary_signal=r1_signal,
                all_signals=[r1_signal],
                combined_confidence=r1_signal.confidence,
            )
            self._cache_result(cache_key, result)
            return result

        # Step 2: Collect probabilistic signals (R2-R5).
        probabilistic_signals: list[ReconciliationSignal] = []

        if r1_signal is not None:
            # R1 matched but with reduced confidence (e.g., normalized match).
            probabilistic_signals.append(r1_signal)

        r2_signal = self.r2.resolve(event_time, entity_id, context.temporal_events)
        if r2_signal is not None:
            probabilistic_signals.append(r2_signal)

        r3_signal = self.r3.resolve(entity_id, context.naming_patterns)
        if r3_signal is not None:
            probabilistic_signals.append(r3_signal)

        historical = context.historical_attributions[-self.execution_config.max_historical_signals :]
        r4_signal = self.r4.resolve(entity_id, historical)
        if r4_signal is not None:
            probabilistic_signals.append(r4_signal)

        r5_signal = self.r5.resolve(
            entity_id,
            context.deployment_owners,
            context.code_owners,
            context.recent_users,
        )
        if r5_signal is not None:
            probabilistic_signals.append(r5_signal)

        # Step 3: Combine signals if we have any.
        if probabilistic_signals:
            if self._exceeded_budget(started):
                result = self._timeboxed_fallback(entity_id, probabilistic_signals)
                self._cache_result(cache_key, result)
                return result

            # Group signals by target entity. Different methods may point
            # to different targets; we need to pick the best cluster.
            target_signals = self._cluster_by_target(probabilistic_signals)
            if len(target_signals) > self.execution_config.max_cluster_targets:
                ranked = sorted(
                    target_signals.items(),
                    key=lambda item: max(signal.confidence for signal in item[1]),
                    reverse=True,
                )[: self.execution_config.max_cluster_targets]
                target_signals = dict(ranked)

            best_target: str | None = None
            best_confidence = 0.0
            best_cluster: list[ReconciliationSignal] = []

            for target, cluster in target_signals.items():
                if self._exceeded_budget(started):
                    break
                combination_input = [(s.method, s.confidence) for s in cluster]
                result = combine_evidence(combination_input, self.combination_config)

                if result.combined_confidence > best_confidence:
                    best_confidence = result.combined_confidence
                    best_target = target
                    best_cluster = cluster

            if best_target is not None and best_confidence > 0.0:
                logger.info(
                    "hre.resolve.probabilistic",
                    target=best_target,
                    confidence=best_confidence,
                    methods=[s.method for s in best_cluster],
                )
                result = self._build_result(
                    entity_id=entity_id,
                    primary_signal=best_cluster[0],
                    all_signals=best_cluster,
                    combined_confidence=best_confidence,
                    alternatives=self._build_alternatives(target_signals, best_target),
                )
                self._cache_result(cache_key, result)
                return result

        # Step 4: Fallback to R6 (proportional allocation).
        r6_signals = self.r6.resolve(entity_id, context.proportional_users)
        if r6_signals:
            # R6 produces fractional attribution across multiple teams.
            best_r6 = max(r6_signals, key=lambda s: s.confidence)
            logger.info(
                "hre.resolve.proportional",
                target=best_r6.target_entity,
                confidence=best_r6.confidence,
                teams=len(r6_signals),
            )
            result = self._build_result(
                entity_id=entity_id,
                primary_signal=best_r6,
                all_signals=r6_signals,
                combined_confidence=best_r6.confidence,
                fractional=[
                    FractionalAttribution(
                        team_id=s.target_entity,
                        team_name=s.target_entity,
                        cost_center_id="",
                        weight=s.feature_values.get("allocation_weight", 0.0),
                        confidence=s.confidence,
                    )
                    for s in r6_signals
                ],
            )
            self._cache_result(cache_key, result)
            return result

        # Step 5: No resolution possible.
        logger.warning("hre.resolve.failed", entity_id=entity_id)
        result = AttributionResult(
            workload_id=entity_id,
            attribution_path=[],
            combined_confidence=0.0,
            explanation=ExplanationArtifact(
                attribution_id=f"attr_{entity_id}",
                target_entity="unresolved",
                confidence_score=0.0,
                method_used="none",
                alternatives_considered=[],
            ),
        )
        self._cache_result(cache_key, result)
        return result

    def _timeboxed_fallback(
        self,
        entity_id: str,
        signals: list[ReconciliationSignal],
    ) -> AttributionResult:
        """Fail-safe reconciliation result when execution budget is exceeded."""
        best = max(signals, key=lambda s: s.confidence)
        return self._build_result(
            entity_id=entity_id,
            primary_signal=best,
            all_signals=[best],
            combined_confidence=best.confidence * 0.9,
            alternatives=[],
        )

    def _exceeded_budget(self, started: float) -> bool:
        elapsed_ms = (time.monotonic() - started) * 1000
        return elapsed_ms > self.execution_config.max_resolve_ms

    def _get_cached(self, key: str) -> AttributionResult | None:
        item = self._cache.get(key)
        if item is None:
            return None
        cached_at, result = item
        if (time.monotonic() - cached_at) > self.execution_config.cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return result

    def _cache_result(self, key: str, result: AttributionResult) -> None:
        self._cache[key] = (time.monotonic(), result)
        self._cache.move_to_end(key)
        while len(self._cache) > self.execution_config.cache_max_entries:
            self._cache.popitem(last=False)

    @staticmethod
    def _cluster_by_target(
        signals: list[ReconciliationSignal],
    ) -> dict[str, list[ReconciliationSignal]]:
        """Group signals by their target entity."""
        clusters: dict[str, list[ReconciliationSignal]] = {}
        for signal in signals:
            target = signal.target_entity
            if target not in clusters:
                clusters[target] = []
            clusters[target].append(signal)
        return clusters

    @staticmethod
    def _build_alternatives(
        target_signals: dict[str, list[ReconciliationSignal]],
        best_target: str,
    ) -> list[dict]:
        """Build alternatives_considered for explanation artifact."""
        alternatives = []
        for target, cluster in target_signals.items():
            if target == best_target:
                continue
            best_conf = max(s.confidence for s in cluster)
            alternatives.append({
                "team_id": target,
                "confidence": best_conf,
                "methods": [s.method for s in cluster],
            })
        return sorted(alternatives, key=lambda x: x["confidence"], reverse=True)

    @staticmethod
    def _build_result(
        entity_id: str,
        primary_signal: ReconciliationSignal,
        all_signals: list[ReconciliationSignal],
        combined_confidence: float,
        alternatives: list[dict] | None = None,
        fractional: list[FractionalAttribution] | None = None,
    ) -> AttributionResult:
        """Construct the full AttributionResult with explanation artifact."""

        path = [
            AttributionPathNode(
                layer="Identity",
                node_id=entity_id,
                node_label=entity_id,
                confidence=combined_confidence,
                method="+".join(s.method for s in all_signals),
                source=", ".join(s.signals[0] if s.signals else s.method for s in all_signals),
            ),
        ]

        explanation = ExplanationArtifact(
            attribution_id=f"attr_{entity_id}_{primary_signal.target_entity}",
            target_entity=primary_signal.target_entity,
            confidence_score=combined_confidence,
            method_used="+".join(s.method for s in all_signals),
            top_contributing_signals=[
                {
                    "method": s.method,
                    "confidence": s.confidence,
                    "signals": s.signals,
                }
                for s in all_signals
            ],
            feature_values={
                s.method: s.feature_values for s in all_signals
            },
            alternatives_considered=alternatives or [],
        )

        return AttributionResult(
            workload_id=entity_id,
            attribution_path=path,
            combined_confidence=combined_confidence,
            explanation=explanation,
            fractional_attributions=fractional or [],
        )
