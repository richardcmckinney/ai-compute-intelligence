"""
Heuristic Reconciliation Engine: R1-R6 reconciliation methods.

The core innovation is not 'having a graph.' It is deriving entity linkages
from dirty, disconnected systems using multi-signal triangulation (Section 4.3).
Probabilistic reconciliation is the PRIMARY attribution method. Deterministic
paths (confidence = 1.0) are a special case, not the expected case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class ReconciliationSignal:
    """A single reconciliation signal from one method."""

    method: str                  # R1-R6
    source_entity: str           # The entity being resolved
    target_entity: str           # The resolved target
    confidence: float            # Raw (pre-calibration) confidence
    signals: list[str] = field(default_factory=list)
    feature_values: dict = field(default_factory=dict)


class R1DirectMatch:
    """
    R1: Direct identifier matching (Section 4.3).

    Deterministic linkage via shared unique identifiers across systems.
    API key -> service account -> code repository -> team.
    Confidence: 1.0 when identifiers match exactly.
    """

    def resolve(
        self,
        entity_id: str,
        identity_mappings: dict[str, str],
    ) -> ReconciliationSignal | None:
        """Attempt direct identifier match across known mappings."""

        # Exact match in identity mapping table.
        if entity_id in identity_mappings:
            return ReconciliationSignal(
                method="R1",
                source_entity=entity_id,
                target_entity=identity_mappings[entity_id],
                confidence=1.0,
                signals=[f"direct_match:{entity_id}"],
                feature_values={"match_type": "exact_identifier"},
            )

        # Normalized match (case-insensitive, strip domain).
        normalized = entity_id.lower().strip()
        for key, target in identity_mappings.items():
            if key.lower().strip() == normalized:
                return ReconciliationSignal(
                    method="R1",
                    source_entity=entity_id,
                    target_entity=target,
                    confidence=0.98,
                    signals=[f"normalized_match:{entity_id}->{key}"],
                    feature_values={"match_type": "normalized_identifier"},
                )

        return None


class R2TemporalCorrelation:
    """
    R2: Temporal correlation (Section 4.3).

    Correlates events across systems by timestamp proximity. If a deployment
    event and a new API key activation occur within a configurable window,
    they are probabilistically linked.

    Confidence varies (0.70-0.95) based on window tightness and competing events.
    """

    def __init__(self, max_window_seconds: float = 300.0) -> None:
        self.max_window_seconds = max_window_seconds

    def resolve(
        self,
        event_time: datetime,
        event_entity: str,
        candidate_events: list[tuple[datetime, str, str]],
    ) -> ReconciliationSignal | None:
        """
        Find temporally correlated events.

        Args:
            event_time: Timestamp of the source event.
            event_entity: Entity identifier from the source event.
            candidate_events: List of (timestamp, entity_id, source_system) tuples
                from other systems to correlate against.
        """
        if not candidate_events:
            return None

        best_match: tuple[float, str, str] | None = None
        competing_count = 0

        for cand_time, cand_entity, cand_source in candidate_events:
            delta = abs((event_time - cand_time).total_seconds())
            if delta <= self.max_window_seconds:
                competing_count += 1
                if best_match is None or delta < best_match[0]:
                    best_match = (delta, cand_entity, cand_source)

        if best_match is None:
            return None

        delta_s, target, source = best_match

        # Confidence decreases with time delta and increases when fewer
        # competing events exist in the window.
        base_confidence = max(0.4, 1.0 - (delta_s / self.max_window_seconds))

        # Penalty for multiple candidates: ambiguity reduces confidence.
        if competing_count > 1:
            ambiguity_penalty = 0.15 * (competing_count - 1)
            base_confidence = max(0.3, base_confidence - ambiguity_penalty)

        return ReconciliationSignal(
            method="R2",
            source_entity=event_entity,
            target_entity=target,
            confidence=round(base_confidence, 3),
            signals=[f"temporal:{source}:{delta_s:.0f}s"],
            feature_values={
                "time_delta_seconds": delta_s,
                "competing_events": competing_count,
                "source_system": source,
            },
        )


class R3NamingConvention:
    """
    R3: Naming convention analysis (Section 4.3).

    Matches service names, repository paths, and resource tags against
    organizational naming patterns. Useful for organizations with consistent
    naming conventions; degrades gracefully when conventions are inconsistent.
    """

    def resolve(
        self,
        entity_name: str,
        candidate_patterns: dict[str, list[str]],
        min_similarity: float = 0.4,
    ) -> ReconciliationSignal | None:
        """
        Match entity name against known naming patterns per team.

        Args:
            entity_name: The name to resolve (e.g., "nlp-experiment-7").
            candidate_patterns: Dict of team_id -> list of known name patterns.
            min_similarity: Minimum string similarity threshold.
        """
        best_score = 0.0
        best_team: str | None = None
        best_pattern: str | None = None

        # Normalize: lowercase, split on common delimiters.
        normalized = self._normalize(entity_name)

        for team_id, patterns in candidate_patterns.items():
            for pattern in patterns:
                norm_pattern = self._normalize(pattern)
                similarity = SequenceMatcher(None, normalized, norm_pattern).ratio()

                # Bonus for substring containment.
                if norm_pattern in normalized or normalized in norm_pattern:
                    similarity = min(1.0, similarity + 0.15)

                # Bonus for matching significant tokens.
                token_overlap = self._token_overlap(normalized, norm_pattern)
                similarity = min(1.0, similarity + token_overlap * 0.1)

                if similarity > best_score:
                    best_score = similarity
                    best_team = team_id
                    best_pattern = pattern

        if best_team is None or best_score < min_similarity:
            return None

        # Scale raw similarity to confidence range.
        # Strong naming match: 0.6-0.85. Perfect substring: up to 0.9.
        confidence = min(0.9, best_score * 0.9)

        return ReconciliationSignal(
            method="R3",
            source_entity=entity_name,
            target_entity=best_team,
            confidence=round(confidence, 3),
            signals=[f"naming:{best_pattern}:{best_score:.2f}"],
            feature_values={
                "string_similarity": round(best_score, 3),
                "matched_pattern": best_pattern,
            },
        )

    @staticmethod
    def _normalize(name: str) -> str:
        return re.sub(r"[^a-z0-9]", " ", name.lower()).strip()

    @staticmethod
    def _token_overlap(a: str, b: str) -> float:
        tokens_a = set(a.split())
        tokens_b = set(b.split())
        if not tokens_a or not tokens_b:
            return 0.0
        return len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))


class R4HistoricalPattern:
    """
    R4: Historical pattern matching (Section 4.3).

    Uses past attribution resolutions as Bayesian priors. If a service account
    has historically been attributed to Team X, new requests from that account
    inherit a prior probability toward Team X, updated by current evidence.
    """

    def resolve(
        self,
        entity_id: str,
        historical_attributions: list[tuple[str, float]],
        min_history_count: int = 3,
    ) -> ReconciliationSignal | None:
        """
        Apply historical pattern matching.

        Args:
            entity_id: The entity to resolve.
            historical_attributions: List of (team_id, confidence) from past resolutions.
            min_history_count: Minimum history entries before applying this method.
        """
        if len(historical_attributions) < min_history_count:
            return None

        # Count attributions per team, weighted by confidence.
        team_scores: dict[str, float] = {}
        team_counts: dict[str, int] = {}
        for team_id, conf in historical_attributions:
            team_scores[team_id] = team_scores.get(team_id, 0.0) + conf
            team_counts[team_id] = team_counts.get(team_id, 0) + 1

        if not team_scores:
            return None

        # Best team is the one with highest weighted score.
        best_team = max(team_scores, key=team_scores.get)  # type: ignore[arg-type]
        total_weight = sum(team_scores.values())
        best_weight = team_scores[best_team]

        # Confidence: proportion of historical evidence pointing to this team.
        dominance = best_weight / total_weight if total_weight > 0 else 0
        count_factor = min(1.0, team_counts[best_team] / 10)  # Saturates at 10 observations.
        confidence = min(0.85, dominance * 0.7 + count_factor * 0.2)

        return ReconciliationSignal(
            method="R4",
            source_entity=entity_id,
            target_entity=best_team,
            confidence=round(confidence, 3),
            signals=[f"historical:{best_team}:{team_counts[best_team]}"],
            feature_values={
                "prior_match_count": team_counts[best_team],
                "dominance_ratio": round(dominance, 3),
                "total_history": len(historical_attributions),
            },
        )


class R5ServiceAccountResolution:
    """
    R5: Service account resolution (Section 4.3).

    Resolves shared service accounts (e.g., svc-analytics) to their most
    likely organizational owner by combining temporal patterns, code ownership,
    and deployment history. Produces lower confidence scores (0.50-0.75)
    reflecting inherent ambiguity.
    """

    def resolve(
        self,
        service_account: str,
        deployment_owners: list[tuple[str, datetime]],
        code_owners: list[str],
        recent_users: list[tuple[str, datetime]],
        lookback: timedelta = timedelta(days=30),
    ) -> ReconciliationSignal | None:
        """
        Resolve a shared service account to its most likely owner.

        Combines multiple weak signals: who deployed it most recently,
        who owns the code it runs, who used it most recently.
        """
        now = datetime.now(timezone.utc)
        scores: dict[str, float] = {}

        # Signal 1: Most recent deployer (strongest signal for service accounts).
        for owner, deploy_time in sorted(deployment_owners, key=lambda x: x[1], reverse=True):
            if now - deploy_time <= lookback:
                scores[owner] = scores.get(owner, 0.0) + 0.35
                break  # Only most recent deployer counts.

        # Signal 2: Code owners (moderate signal).
        for owner in code_owners:
            scores[owner] = scores.get(owner, 0.0) + 0.25

        # Signal 3: Recent users (weak signal, many users may use shared accounts).
        recent_set = set()
        for user, use_time in recent_users:
            if now - use_time <= lookback:
                recent_set.add(user)
        for user in recent_set:
            scores[user] = scores.get(user, 0.0) + 0.15 / max(1, len(recent_set))

        if not scores:
            return None

        best_owner = max(scores, key=scores.get)  # type: ignore[arg-type]

        # Confidence capped at 0.75 for service account resolution.
        confidence = min(0.75, scores[best_owner])

        return ReconciliationSignal(
            method="R5",
            source_entity=service_account,
            target_entity=best_owner,
            confidence=round(confidence, 3),
            signals=[
                f"svc_account:{service_account}",
                f"deployer:{len(deployment_owners)}",
                f"code_owners:{len(code_owners)}",
                f"recent_users:{len(recent_set)}",
            ],
            feature_values={
                "signal_count": len(scores),
                "best_score": round(scores[best_owner], 3),
                "is_shared_account": True,
            },
        )


class R6ProportionalAllocation:
    """
    R6: Proportional allocation (Section 4.3).

    Fallback method. Distributes costs proportionally across teams known to
    use a shared resource, weighted by historical usage patterns. Lowest
    confidence method; used only when R1-R5 cannot resolve ownership.
    """

    def resolve(
        self,
        resource_id: str,
        known_users: dict[str, float],
    ) -> list[ReconciliationSignal]:
        """
        Proportionally allocate a resource across known users.

        Args:
            resource_id: The shared resource to allocate.
            known_users: Dict of team_id -> historical usage weight (0..1).

        Returns:
            List of signals, one per team, representing fractional attribution.
        """
        if not known_users:
            return []

        total_weight = sum(known_users.values())
        if total_weight <= 0:
            return []

        signals = []
        for team_id, weight in known_users.items():
            proportion = weight / total_weight
            # Confidence is low and scales with how dominant one team is.
            # If one team accounts for 90% of usage, confidence is higher.
            confidence = min(0.65, 0.3 + proportion * 0.4)

            signals.append(ReconciliationSignal(
                method="R6",
                source_entity=resource_id,
                target_entity=team_id,
                confidence=round(confidence, 3),
                signals=[f"proportional:{team_id}:{proportion:.2f}"],
                feature_values={
                    "allocation_weight": round(proportion, 3),
                    "team_count": len(known_users),
                },
            ))

        return signals
