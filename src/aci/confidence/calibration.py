"""
Confidence calibration engine (Patent Spec Section 5.2).

Achieves empirical calibration: a confidence score of 0.8 means approximately
80% of attributions at that score were correct when validated against ground truth.

Calibration method: per-method reliability curves via isotonic regression.
Each reconciliation method (R1-R6) has its own curve updated weekly.

Cold-start handling: warm-start curves from synthetic enterprise environments
are progressively replaced as customer-specific ground truth accumulates.
Transition triggered when KS statistic > 0.15 (configurable).
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import structlog
from scipy.stats import ks_2samp
from sklearn.isotonic import IsotonicRegression

from aci.config import ConfidenceConfig
from aci.models.confidence import (
    CalibrationCurve,
    ConfidenceTier,
    GroundTruthLabel,
)

logger = structlog.get_logger()

# Pre-calibrated warm-start curves derived from synthetic enterprise environments
# modeled on common identity/CI/CD/billing topologies (Section 5.2).
# These carry wider uncertainty bands and are explicitly labeled 'pre-calibrated'.
WARM_START_CURVES: dict[str, list[tuple[float, float]]] = {
    "R1": [(0.0, 0.0), (0.5, 0.5), (0.9, 0.92), (0.98, 0.98), (1.0, 1.0)],
    "R2": [(0.0, 0.0), (0.3, 0.22), (0.5, 0.40), (0.7, 0.58), (0.9, 0.82), (1.0, 0.90)],
    "R3": [(0.0, 0.0), (0.3, 0.18), (0.5, 0.35), (0.7, 0.52), (0.9, 0.75), (1.0, 0.85)],
    "R4": [(0.0, 0.0), (0.3, 0.20), (0.5, 0.38), (0.7, 0.55), (0.9, 0.78), (1.0, 0.88)],
    "R5": [(0.0, 0.0), (0.3, 0.15), (0.5, 0.30), (0.7, 0.48), (0.9, 0.68), (1.0, 0.75)],
    "R6": [(0.0, 0.0), (0.3, 0.12), (0.5, 0.25), (0.7, 0.40), (0.9, 0.58), (1.0, 0.65)],
}


class CalibrationEngine:
    """
    Manages per-method calibration curves and applies calibration to raw scores.

    Lifecycle:
    1. Initialize with warm-start curves.
    2. Accumulate ground truth labels from CI/CD provenance, user corrections,
       FinOps review decisions, and ticket-based approvals.
    3. When sample count reaches threshold, fit isotonic regression per method.
    4. When customer-specific curve diverges from warm-start (KS > threshold),
       transition to customer-calibrated curve.
    """

    def __init__(self, config: ConfidenceConfig | None = None) -> None:
        self.config = config or ConfidenceConfig()
        self.curves: dict[str, CalibrationCurve] = {}
        self.ground_truth: dict[str, list[GroundTruthLabel]] = defaultdict(list)
        self._iso_models: dict[str, IsotonicRegression] = {}

        # Initialize warm-start curves.
        for method, points in WARM_START_CURVES.items():
            raw = [p[0] for p in points]
            calibrated = [p[1] for p in points]
            self.curves[method] = CalibrationCurve(
                method=method,
                raw_scores=raw,
                calibrated_scores=calibrated,
                sample_count=0,
                is_warm_start=True,
            )

    def calibrate(self, method: str, raw_confidence: float) -> float:
        """
        Apply calibration to a raw confidence score.

        Returns the calibrated confidence, which represents the empirical
        probability that the attribution is correct at this score level.
        """
        if method not in self.curves:
            # Unknown method: return raw score with a conservative discount.
            return raw_confidence * 0.7

        curve = self.curves[method]

        # If we have a fitted isotonic model, use it.
        if method in self._iso_models:
            calibrated = float(self._iso_models[method].predict([raw_confidence])[0])
            return min(self.config.cap, max(0.0, calibrated))

        # Otherwise, interpolate from the curve points.
        return self._interpolate(curve.raw_scores, curve.calibrated_scores, raw_confidence)

    def calibrate_score(self, method: str, raw_confidence: float) -> float:
        """
        Backward-compatible wrapper for older call sites.

        The canonical entrypoint is ``calibrate``.
        """
        return self.calibrate(method, raw_confidence)

    def add_ground_truth(self, label: GroundTruthLabel) -> None:
        """
        Record a ground truth observation for the calibration loop.

        Sources (Section 5.2):
        - CI/CD provenance (deterministic path as label for nearby probabilistic)
        - Explicit user corrections in UI
        - FinOps review decisions (approve/reject chargeback)
        - Ticket-based approvals
        """
        self.ground_truth[label.method_used].append(label)

        # Check if we should refit.
        method = label.method_used
        count = len(self.ground_truth[method])

        if count >= self.config.min_samples_full_calibration:
            self._fit_isotonic(method)
        elif count >= self.config.min_samples_bootstrap:
            self._fit_bootstrap(method)

    def _fit_isotonic(self, method: str) -> None:
        """
        Fit isotonic regression when we have sufficient samples (>=200).

        Isotonic regression is simple and well-understood (Section 5.2).
        It requires sufficient tail coverage for stable curves.
        """
        labels = self.ground_truth[method]
        raw_scores = np.array([label.predicted_confidence for label in labels])
        outcomes = np.array([1.0 if label.was_correct else 0.0 for label in labels])

        iso = IsotonicRegression(y_min=0.0, y_max=self.config.cap, out_of_bounds="clip")
        iso.fit(raw_scores, outcomes)
        self._iso_models[method] = iso

        # Generate curve points for storage and visualization.
        test_points = np.linspace(0.0, 1.0, 50)
        calibrated_points = iso.predict(test_points)

        customer_curve = CalibrationCurve(
            method=method,
            raw_scores=test_points.tolist(),
            calibrated_scores=calibrated_points.tolist(),
            sample_count=len(labels),
            is_warm_start=False,
        )

        # Check KS statistic against warm-start for transition logging.
        if method in WARM_START_CURVES:
            warm_calibrated = np.array(
                [
                    self._interpolate(
                        [p[0] for p in WARM_START_CURVES[method]],
                        [p[1] for p in WARM_START_CURVES[method]],
                        x,
                    )
                    for x in test_points
                ]
            )
            ks_stat, _ = ks_2samp(calibrated_points, warm_calibrated)
            customer_curve.ks_statistic = float(ks_stat)

            if ks_stat > self.config.warmstart_ks_threshold:
                logger.info(
                    "calibration.transition",
                    method=method,
                    ks_statistic=ks_stat,
                    sample_count=len(labels),
                )

        self.curves[method] = customer_curve

    def _fit_bootstrap(self, method: str) -> None:
        """
        Provide provisional calibration with bootstrap confidence intervals
        when sample count is between 50 and 200 (Section 5.2).
        """
        labels = self.ground_truth[method]
        raw_scores = np.array([label.predicted_confidence for label in labels])
        outcomes = np.array([1.0 if label.was_correct else 0.0 for label in labels])

        # Bootstrap: resample 100 times, fit isotonic each time.
        n_bootstrap = 100
        test_points = np.linspace(0.0, 1.0, 20)
        bootstrap_curves = np.zeros((n_bootstrap, len(test_points)))

        rng = np.random.default_rng(42)
        for b in range(n_bootstrap):
            indices = rng.choice(len(labels), size=len(labels), replace=True)
            iso = IsotonicRegression(y_min=0.0, y_max=self.config.cap, out_of_bounds="clip")
            iso.fit(raw_scores[indices], outcomes[indices])
            bootstrap_curves[b] = iso.predict(test_points)

        median_curve = np.median(bootstrap_curves, axis=0)
        lower = np.percentile(bootstrap_curves, 2.5, axis=0)
        upper = np.percentile(bootstrap_curves, 97.5, axis=0)

        self.curves[method] = CalibrationCurve(
            method=method,
            raw_scores=test_points.tolist(),
            calibrated_scores=median_curve.tolist(),
            sample_count=len(labels),
            is_warm_start=True,  # Still provisional.
            uncertainty_band=(float(np.mean(lower)), float(np.mean(upper))),
        )

    @staticmethod
    def _interpolate(
        raw_points: list[float],
        cal_points: list[float],
        value: float,
    ) -> float:
        """Linear interpolation between curve points."""
        if value <= raw_points[0]:
            return cal_points[0]
        if value >= raw_points[-1]:
            return cal_points[-1]

        for i in range(len(raw_points) - 1):
            if raw_points[i] <= value <= raw_points[i + 1]:
                t = (value - raw_points[i]) / (raw_points[i + 1] - raw_points[i])
                return cal_points[i] + t * (cal_points[i + 1] - cal_points[i])

        return value  # Fallback: return raw.

    def get_confidence_tier(self, confidence: float) -> ConfidenceTier:
        """Classify a confidence score into operational tiers (Section 5.1)."""
        if confidence >= self.config.chargeback_threshold:
            return ConfidenceTier.CHARGEBACK_READY
        elif confidence >= self.config.provisional_threshold:
            return ConfidenceTier.PROVISIONAL
        else:
            return ConfidenceTier.ESTIMATED

    def apply_temporal_decay(
        self,
        confidence: float,
        signal_age_days: float,
    ) -> float:
        """
        Apply temporal decay to confidence (Section 7).

        Confidence scores from signals older than the recency window are
        discounted to prevent stale attributions from suppressing risk premium.
        """
        if signal_age_days <= self.config.decay_window_days:
            return confidence

        excess_days = signal_age_days - self.config.decay_window_days
        decay = math.exp(-self.config.decay_rate * excess_days)
        return confidence * decay
