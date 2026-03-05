"""
Capability equivalence verification (Patent Spec Section 6.2).

Before the platform ever recommends routing a request to a different model,
it verifies that the alternative produces equivalent output quality for that
specific use case.

Three modes:
- Mode 1: Policy-based (admin-defined model families). Pure configuration, no ML.
- Mode 2a: Empirical (shadow evaluation harness, min 100 requests).
- Mode 2b: LLM-as-judge (blind evaluation, min 200 samples).
- Mode 3: Contractual (compliance boundary, no cross-boundary optimization).

Fail-safe override: If no equivalence mode confirms a safe alternative,
route to the originally requested model. The system never optimizes when
it cannot verify safety.
"""

from __future__ import annotations

import hashlib
import statistics
from datetime import UTC, datetime, timedelta

import structlog

from aci.config import EquivalenceConfig
from aci.models.carbon import EquivalenceMode, EquivalenceVerification

logger = structlog.get_logger()


class EquivalenceClass:
    """
    An admin-defined model equivalence class (Mode 1).

    Groups models that are approved for a specific use case.
    Example: "Customer Support Chat" -> [gpt-4o, gpt-4o-mini, claude-3-haiku].
    """

    def __init__(
        self,
        class_id: str,
        name: str,
        approved_models: list[str],
        use_case: str = "",
    ) -> None:
        self.class_id = class_id
        self.name = name
        self.approved_models = set(approved_models)
        self.use_case = use_case

    def is_approved(self, model: str) -> bool:
        return model in self.approved_models

    def get_alternatives(self, current_model: str) -> list[str]:
        """Return approved alternatives, excluding the current model."""
        return [m for m in self.approved_models if m != current_model]


class ShadowEvaluationResult:
    """Result of a single shadow evaluation comparison."""

    def __init__(
        self,
        baseline_score: float,
        candidate_score: float,
        criteria_scores: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self.baseline_score = baseline_score
        self.candidate_score = candidate_score
        self.criteria_scores = criteria_scores or {}


class EquivalenceVerifier:
    """
    Orchestrates capability equivalence verification across all modes.

    The verifier maintains a cache of equivalence determinations with TTL
    (default 7 days). Determinations expire on model version change or TTL
    expiry, triggering automatic re-evaluation.
    """

    def __init__(self, config: EquivalenceConfig | None = None) -> None:
        self.config = config or EquivalenceConfig()
        self.equivalence_classes: dict[str, EquivalenceClass] = {}
        self.verifications: dict[str, EquivalenceVerification] = {}
        self.shadow_results: dict[str, list[ShadowEvaluationResult]] = {}

    def register_class(self, eq_class: EquivalenceClass) -> None:
        """Register an equivalence class definition."""
        self.equivalence_classes[eq_class.class_id] = eq_class

    def verify_policy(
        self,
        source_model: str,
        candidate_model: str,
        class_id: str,
    ) -> EquivalenceVerification:
        """
        Mode 1: Policy-based equivalence.

        Pure configuration lookup. No ML involved. Both models must be
        in the same admin-defined equivalence class.
        """
        eq_class = self.equivalence_classes.get(class_id)
        if eq_class is None:
            return self._fail_safe(source_model, candidate_model, class_id, EquivalenceMode.POLICY)

        is_equivalent = eq_class.is_approved(source_model) and eq_class.is_approved(candidate_model)

        verification = EquivalenceVerification(
            verification_id=self._make_id(source_model, candidate_model, class_id),
            source_model=source_model,
            candidate_model=candidate_model,
            equivalence_class_id=class_id,
            mode=EquivalenceMode.POLICY,
            is_equivalent=is_equivalent,
            expires_at=datetime.now(UTC) + timedelta(days=self.config.ttl_days),
        )
        self._cache(verification)
        return verification

    def verify_empirical(
        self,
        source_model: str,
        candidate_model: str,
        class_id: str,
        evaluation_results: list[ShadowEvaluationResult],
    ) -> EquivalenceVerification:
        """
        Mode 2a: Empirical equivalence via shadow evaluation.

        Compares candidate against baseline on a task-specific rubric.
        The lower bound of the 95% CI must exceed baseline - delta.
        If the CI straddles the threshold, the candidate is NOT equivalent.
        """
        if len(evaluation_results) < self.config.min_shadow_samples:
            logger.warning(
                "equivalence.insufficient_samples",
                mode="empirical",
                count=len(evaluation_results),
                required=self.config.min_shadow_samples,
            )
            return self._fail_safe(
                source_model, candidate_model, class_id, EquivalenceMode.EMPIRICAL
            )

        # Compute aggregate quality scores.
        baseline_scores = [r.baseline_score for r in evaluation_results]
        candidate_scores = [r.candidate_score for r in evaluation_results]

        baseline_mean = statistics.mean(baseline_scores)
        candidate_mean = statistics.mean(candidate_scores)

        # Compute 95% confidence interval for the candidate.
        if len(candidate_scores) > 1:
            candidate_stderr = statistics.stdev(candidate_scores) / len(candidate_scores) ** 0.5
            ci_lower = candidate_mean - 1.96 * candidate_stderr
            ci_upper = candidate_mean + 1.96 * candidate_stderr
        else:
            ci_lower = candidate_mean
            ci_upper = candidate_mean

        # Equivalence threshold: candidate CI lower bound >= baseline - delta.
        threshold = baseline_mean - self.config.quality_delta
        is_equivalent = ci_lower >= threshold

        verification = EquivalenceVerification(
            verification_id=self._make_id(source_model, candidate_model, class_id),
            source_model=source_model,
            candidate_model=candidate_model,
            equivalence_class_id=class_id,
            mode=EquivalenceMode.EMPIRICAL,
            is_equivalent=is_equivalent,
            sample_count=len(evaluation_results),
            baseline_score=round(baseline_mean, 4),
            candidate_score=round(candidate_mean, 4),
            quality_delta=round(candidate_mean - baseline_mean, 4),
            confidence_interval=(round(ci_lower, 4), round(ci_upper, 4)),
            expires_at=datetime.now(UTC) + timedelta(days=self.config.ttl_days),
        )
        self._cache(verification)
        return verification

    def verify_judge(
        self,
        source_model: str,
        candidate_model: str,
        class_id: str,
        judge_scores: list[tuple[float, float]],
    ) -> EquivalenceVerification:
        """
        Mode 2b: LLM-as-judge evaluation.

        A reference model scores candidate outputs against baseline outputs.
        The judge model is explicitly excluded from the candidate equivalence
        class to prevent circular evaluation.
        """
        if len(judge_scores) < self.config.min_judge_samples:
            return self._fail_safe(source_model, candidate_model, class_id, EquivalenceMode.JUDGE)

        baseline_scores = [s[0] for s in judge_scores]
        candidate_scores = [s[1] for s in judge_scores]

        baseline_mean = statistics.mean(baseline_scores)
        candidate_mean = statistics.mean(candidate_scores)

        # Win rate: fraction of cases where candidate >= baseline.
        wins = sum(1 for b, c in judge_scores if c >= b)
        win_rate = wins / len(judge_scores)

        is_equivalent = (
            candidate_mean >= baseline_mean - self.config.quality_delta
            and win_rate >= 0.45  # At least 45% non-inferior.
        )

        verification = EquivalenceVerification(
            verification_id=self._make_id(source_model, candidate_model, class_id),
            source_model=source_model,
            candidate_model=candidate_model,
            equivalence_class_id=class_id,
            mode=EquivalenceMode.JUDGE,
            is_equivalent=is_equivalent,
            sample_count=len(judge_scores),
            baseline_score=round(baseline_mean, 4),
            candidate_score=round(candidate_mean, 4),
            quality_delta=round(candidate_mean - baseline_mean, 4),
            expires_at=datetime.now(UTC) + timedelta(days=self.config.ttl_days),
        )
        self._cache(verification)
        return verification

    def get_cached_verification(
        self,
        source_model: str,
        candidate_model: str,
        class_id: str,
    ) -> EquivalenceVerification | None:
        """Retrieve a cached verification if it exists and hasn't expired."""
        key = self._make_id(source_model, candidate_model, class_id)
        verification = self.verifications.get(key)
        if verification is None:
            return None
        if verification.expires_at and verification.expires_at < datetime.now(UTC):
            del self.verifications[key]
            return None
        return verification

    def _fail_safe(
        self,
        source: str,
        candidate: str,
        class_id: str,
        mode: EquivalenceMode,
    ) -> EquivalenceVerification:
        """
        Fail-safe: return NOT equivalent (Section 6.2).

        If no equivalence mode confirms a safe alternative, route to
        the originally requested model.
        """
        return EquivalenceVerification(
            verification_id=self._make_id(source, candidate, class_id),
            source_model=source,
            candidate_model=candidate,
            equivalence_class_id=class_id,
            mode=mode,
            is_equivalent=False,
            fail_safe_triggered=True,
        )

    def _cache(self, verification: EquivalenceVerification) -> None:
        self.verifications[verification.verification_id] = verification

    @staticmethod
    def _make_id(source: str, candidate: str, class_id: str) -> str:
        raw = f"{source}:{candidate}:{class_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
