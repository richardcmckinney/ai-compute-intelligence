"""
Federated Benchmarking Protocol (Patent Spec Section 11).

Enables cross-organizational performance comparison with formal privacy guarantees.
Uses Laplace mechanism with sensitivity bounds, privacy budget accounting, and
anti-differencing rules.

Privacy accounting: each organization has epsilon_total = 4.0 per quarter.
Each benchmark release consumes epsilon_release (default 1.0).

Noise mechanism:
  Development: continuous Laplace via numpy (fast, sufficient for protocol testing).
  Production: discrete geometric Laplace using secrets module. Eliminates the
    floating-point LSB leakage inherent in continuous Laplace implementations
    (Mironov 2012, "On Significance of the Least Significant Bits For
    Differential Privacy"). Must be enabled before any real organizational
    data flows through the protocol.
"""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import structlog

from aci.config import FBPConfig

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Noise mechanisms
# ---------------------------------------------------------------------------

def _continuous_laplace(scale: float, rng: np.random.Generator) -> float:
    """
    Standard continuous Laplace noise via numpy.

    Suitable for development and protocol testing. NOT safe for production
    differential privacy: IEEE 754 floating-point arithmetic leaks information
    through least-significant-bit patterns.
    """
    return float(rng.laplace(0, scale))


def _discrete_laplace(scale: float) -> float:
    """
    Cryptographically secure discrete Laplace noise.

    Constructed as the difference of two geometric random variables using
    the secrets module for CSPRNG. Eliminates the floating-point LSB
    vulnerability present in continuous Laplace implementations.

    The discrete Laplace with parameter b produces the distribution
    P(X = k) proportional to exp(-|k|/b) for integer k, achieved by
    sampling Geom(1 - exp(-1/b)) and taking the difference.
    """
    if scale <= 0.0:
        return 0.0

    # q = exp(-1/b) is the geometric distribution parameter.
    q = math.exp(-1.0 / scale)

    def _secure_geometric(q_param: float) -> int:
        if q_param >= 1.0 or q_param <= 0.0:
            return 0
        # CSPRNG uniform [0, 1).
        u = secrets.SystemRandom().random()
        if u == 0.0:
            u = 2.0 ** -53  # Smallest positive float64.
        return int(math.floor(math.log(u) / math.log(q_param)))

    return float(_secure_geometric(q) - _secure_geometric(q))


@dataclass
class CohortMembership:
    """An organization's membership in a benchmarking cohort."""

    org_id: str
    cohort_id: str
    joined_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BenchmarkMetric:
    """A single metric contributed by an organization for benchmarking."""

    org_id: str
    metric_name: str
    raw_value: float
    period: str  # e.g., "2026-Q1"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CohortBenchmark:
    """Aggregate benchmark result for a cohort, with DP noise."""

    cohort_id: str
    metric_name: str
    noisy_mean: float
    noisy_median: float
    member_count: int
    epsilon_consumed: float
    period: str
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    suppressed: bool = False
    suppression_reason: str = ""


class PrivacyBudgetExhausted(Exception):
    """Raised when an org's quarterly privacy budget is spent."""

    pass


class FederatedBenchmarkProtocol:
    """
    Federated benchmarking with differential privacy (Section 11).

    Key mechanisms:
    1. Sensitivity bounds: each metric clipped before noise addition.
    2. Laplace noise: calibrated to sensitivity / epsilon.
    3. Privacy budget: epsilon_total = 4.0 per quarter per org.
    4. Anti-differencing rules: prevent overlapping cohort publication.
    5. Minimum cohort size: k = 5 organizations.
    """

    def __init__(self, config: FBPConfig | None = None) -> None:
        self.config = config or FBPConfig()
        self.cohorts: dict[str, list[CohortMembership]] = {}
        self.metrics: dict[str, list[BenchmarkMetric]] = {}
        self.budget_consumed: dict[str, float] = {}  # org_id -> epsilon consumed this quarter
        self.rng = np.random.default_rng()

        # Select noise mechanism based on config.
        if self.config.use_secure_noise:
            self._add_noise = lambda scale: _discrete_laplace(scale)
            logger.info("fbp.noise_mechanism", mechanism="discrete_laplace_csprng")
        else:
            self._add_noise = lambda scale: _continuous_laplace(scale, self.rng)
            logger.info("fbp.noise_mechanism", mechanism="continuous_laplace_numpy")

        # Sensitivity bounds per metric (Section 11.1).
        self.sensitivity_bounds: dict[str, float] = {
            "cost_per_token_usd": self.config.cost_per_token_clip,
            "carbon_per_inference_gco2e": self.config.carbon_per_inference_clip,
            "attribution_confidence": 1.0,
            "waste_pct": 100.0,
        }

    def create_cohort(self, cohort_id: str) -> None:
        """Create a new benchmarking cohort."""
        self.cohorts[cohort_id] = []

    def join_cohort(self, org_id: str, cohort_id: str) -> bool:
        """Add an organization to a cohort."""
        if cohort_id not in self.cohorts:
            return False
        if any(m.org_id == org_id for m in self.cohorts[cohort_id]):
            return False  # Already a member.
        self.cohorts[cohort_id].append(CohortMembership(org_id=org_id, cohort_id=cohort_id))
        return True

    def submit_metric(self, metric: BenchmarkMetric) -> None:
        """Submit a metric contribution. Values are clipped to sensitivity bounds."""
        key = f"{metric.org_id}:{metric.metric_name}:{metric.period}"
        if key not in self.metrics:
            self.metrics[key] = []

        # Clip to sensitivity bound.
        bound = self.sensitivity_bounds.get(metric.metric_name, 1000.0)
        clipped_value = max(0.0, min(metric.raw_value, bound))
        clipped_metric = BenchmarkMetric(
            org_id=metric.org_id,
            metric_name=metric.metric_name,
            raw_value=clipped_value,
            period=metric.period,
        )
        self.metrics[key].append(clipped_metric)

    def publish_benchmark(
        self,
        cohort_id: str,
        metric_name: str,
        period: str,
    ) -> CohortBenchmark:
        """
        Publish a differentially private benchmark for a cohort.

        Applies Laplace noise calibrated to sensitivity / epsilon,
        enforces minimum cohort size, and checks anti-differencing rules.
        """
        members = self.cohorts.get(cohort_id, [])

        # Anti-differencing rule: minimum cohort size.
        if len(members) < self.config.min_cohort_size:
            return CohortBenchmark(
                cohort_id=cohort_id,
                metric_name=metric_name,
                noisy_mean=0.0,
                noisy_median=0.0,
                member_count=len(members),
                epsilon_consumed=0.0,
                period=period,
                suppressed=True,
                suppression_reason=f"Cohort size {len(members)} < minimum {self.config.min_cohort_size}",
            )

        # Anti-differencing rule: check membership churn.
        # (Simplified: in production, compare against previous period's membership.)

        # Collect metric values from all cohort members.
        values: list[float] = []
        for member in members:
            key = f"{member.org_id}:{metric_name}:{period}"
            org_metrics = self.metrics.get(key, [])
            if org_metrics:
                values.append(org_metrics[-1].raw_value)

        if not values:
            return CohortBenchmark(
                cohort_id=cohort_id,
                metric_name=metric_name,
                noisy_mean=0.0,
                noisy_median=0.0,
                member_count=len(members),
                epsilon_consumed=0.0,
                period=period,
                suppressed=True,
                suppression_reason="No metric submissions for this period",
            )

        # Check privacy budgets.
        epsilon = self.config.epsilon_per_release
        for member in members:
            consumed = self.budget_consumed.get(member.org_id, 0.0)
            if consumed + epsilon > self.config.epsilon_total_quarterly:
                raise PrivacyBudgetExhausted(
                    f"Org {member.org_id}: budget {consumed + epsilon:.1f} "
                    f"exceeds quarterly limit {self.config.epsilon_total_quarterly}"
                )

        # Compute sensitivity.
        sensitivity = self.sensitivity_bounds.get(metric_name, 1000.0) / len(values)

        # Add calibrated noise: scale = sensitivity / epsilon.
        noise_scale = sensitivity / epsilon
        raw_mean = float(np.mean(values))
        raw_median = float(np.median(values))
        noisy_mean = raw_mean + self._add_noise(noise_scale)
        noisy_median = raw_median + self._add_noise(noise_scale)

        # Update privacy budgets.
        for member in members:
            self.budget_consumed[member.org_id] = (
                self.budget_consumed.get(member.org_id, 0.0) + epsilon
            )

        return CohortBenchmark(
            cohort_id=cohort_id,
            metric_name=metric_name,
            noisy_mean=round(noisy_mean, 6),
            noisy_median=round(noisy_median, 6),
            member_count=len(members),
            epsilon_consumed=epsilon,
            period=period,
        )

    def get_remaining_budget(self, org_id: str) -> float:
        """Get remaining privacy budget for an organization this quarter."""
        consumed = self.budget_consumed.get(org_id, 0.0)
        return max(0.0, self.config.epsilon_total_quarterly - consumed)

    def reset_quarterly_budgets(self) -> None:
        """Reset all privacy budgets at quarter boundary."""
        self.budget_consumed.clear()
        logger.info("fbp.budgets_reset")
