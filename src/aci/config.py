"""
Platform configuration.

All configurable parameters referenced in the patent specification are centralized here
with their documented defaults and rationale. Customer-specific overrides are loaded
from environment variables or a configuration service.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class InterceptorConfig(BaseSettings):
    """Fail-open interceptor configuration (Patent Spec Section 6.1)."""

    model_config = {"env_prefix": "ACI_INTERCEPTOR_"}

    # Hard timeout budget: 20ms for enrichment lookup.
    # The remaining 30ms of the 50ms total budget is reserved for routing.
    timeout_ms: int = Field(default=20, description="Hard timeout for index lookup (ms)")

    # Total interception budget including routing decision.
    total_budget_ms: int = Field(default=50, description="Total interception overhead budget (ms)")

    # Circuit breaker: consecutive failures before opening.
    circuit_breaker_threshold: int = Field(default=5, description="Failures before circuit opens")

    # Circuit breaker: seconds before attempting half-open.
    circuit_breaker_reset_s: float = Field(default=30.0, description="Seconds before half-open")

    # Circuit breaker state backend.
    # local: per-process memory (development/testing)
    # redis: shared state across horizontally scaled pods
    circuit_state_backend: str = Field(
        default="local",
        description="Circuit breaker state backend (local|redis)",
    )
    circuit_state_redis_key: str = Field(
        default="aci:circuit:interceptor",
        description="Redis key for shared circuit breaker state",
    )

    # Shadow warming: probability of triggering background refresh on cache miss.
    # Prevents thundering herd (Section 6.3).
    shadow_warm_probability: float = Field(default=0.01, description="P(trigger refresh)")

    # Emit shadow events to the event bus on miss/timeout.
    shadow_events_enabled: bool = Field(default=True)

    # Stale-while-revalidate: serve last known entry during refresh.
    stale_while_revalidate: bool = Field(default=True)


class ConfidenceConfig(BaseSettings):
    """Confidence governance configuration (Patent Spec Section 5)."""

    model_config = {"env_prefix": "ACI_CONFIDENCE_"}

    # Combined confidence cap (Section 5.3a).
    # Rationale: 2-5% data quality error rate in enterprise identity systems.
    cap: float = Field(default=0.95, description="Max combined confidence score")

    # Diminishing returns: third signal adds at most 50% of second (Section 5.3b).
    diminishing_factor: float = Field(default=0.5, description="Diminishing returns per signal")

    # Default dependency discount for correlated signals (Section 5.3c).
    # Initial value; adjusted per method pair as ground truth accumulates.
    default_dependency_discount: float = Field(default=0.5, description="Correlated signal discount")

    # Operational thresholds (Section 5.1).
    chargeback_threshold: float = Field(default=0.80, description="Min confidence for chargeback")
    provisional_threshold: float = Field(default=0.50, description="Min confidence for dashboard")

    # Calibration requirements (Section 5.2).
    min_samples_full_calibration: int = Field(default=200, description="Samples for isotonic")
    min_samples_bootstrap: int = Field(default=50, description="Samples for bootstrap CI")

    # Warm-start transition threshold (Section 5.2).
    # KS statistic threshold for switching from pre-calibrated to customer-calibrated.
    warmstart_ks_threshold: float = Field(default=0.15, description="KS stat for curve transition")

    # Temporal decay (Section 7).
    decay_window_days: int = Field(default=90, description="Recency window for decay")
    decay_rate: float = Field(default=0.01, description="Daily decay factor")


class TRACConfig(BaseSettings):
    """TRAC metric configuration (Patent Spec Section 7)."""

    model_config = {"env_prefix": "ACI_TRAC_"}

    # Default internal carbon price ($/tCO2e).
    # Updated quarterly from EMBER Carbon Price Tracker.
    carbon_price_per_tco2e: float = Field(default=50.0, description="Internal carbon price")

    # Default risk multiplier for confidence risk premium.
    # Represents estimated 15% average dispute cost for misallocated workloads.
    risk_multiplier: float = Field(default=0.15, description="Chargeback dispute cost factor")


class CarbonConfig(BaseSettings):
    """Carbon ledger configuration (Patent Spec Section 8)."""

    model_config = {"env_prefix": "ACI_CARBON_"}

    # Default embodied carbon hardware lifetime (Section 8.1).
    hardware_lifetime_years: float = Field(default=4.0, description="GPU useful life (years)")
    hardware_utilization: float = Field(default=0.85, description="Expected utilization rate")

    # Default methodology layer for new deployments.
    default_layer: int = Field(default=2, description="Default carbon methodology layer")

    # Embodied carbon: tokens-to-GPU-second conversion for online inference.
    # Override per model profile when real telemetry is available.
    # This is an estimate; batch and training workloads should provide actual
    # measured_gpu_seconds instead of relying on this default.
    tokens_per_gpu_second: float = Field(
        default=500.0,
        description="Tokens processed per GPU-second (default for online inference)",
    )

    # A100-80GB manufacturing embodied carbon estimate (kgCO2e).
    # Source: Gupta et al. 2022, "Chasing Carbon," ACM ASPLOS.
    embodied_carbon_manufacturing_kg: float = Field(
        default=150.0,
        description="GPU manufacturing embodied carbon (kgCO2e)",
    )


class EquivalenceConfig(BaseSettings):
    """Capability equivalence configuration (Patent Spec Section 6.2)."""

    model_config = {"env_prefix": "ACI_EQUIV_"}

    # Mode 2a: minimum sample size for empirical evaluation.
    min_shadow_samples: int = Field(default=100, description="Min requests for shadow eval")

    # Mode 2b: minimum sample size for LLM-as-judge.
    min_judge_samples: int = Field(default=200, description="Min samples for judge eval")

    # Equivalence determination TTL (days).
    ttl_days: int = Field(default=7, description="Days before re-evaluation required")

    # Quality threshold delta: candidate must score >= baseline - delta.
    quality_delta: float = Field(default=0.05, description="Allowed quality degradation")


class FBPConfig(BaseSettings):
    """Federated Benchmarking Protocol configuration (Patent Spec Section 11)."""

    model_config = {"env_prefix": "ACI_FBP_"}

    # Privacy budget per quarter (Section 11.1).
    epsilon_total_quarterly: float = Field(default=4.0, description="Total epsilon per quarter")
    epsilon_per_release: float = Field(default=1.0, description="Epsilon per benchmark release")

    # Sensitivity bounds for Laplace mechanism (Section 11.1).
    cost_per_token_clip: float = Field(default=0.10, description="Max cost/token before clipping")
    carbon_per_inference_clip: float = Field(default=100.0, description="Max gCO2e before clipping")

    # Minimum cohort size (Section 11.1).
    min_cohort_size: int = Field(default=5, description="Min orgs per cohort")

    # Noise mechanism selection.
    # False (default): continuous Laplace via numpy. Sufficient for protocol testing.
    # True: discrete geometric Laplace via secrets CSPRNG. Required before any
    # real organizational data flows through the protocol. See Mironov 2012.
    use_secure_noise: bool = Field(default=False, description="Use discrete Laplace (production)")

    # Max membership churn for cohort publication.
    max_churn_rate: float = Field(default=0.20, description="Max month-over-month churn")


class PlatformConfig(BaseSettings):
    """Top-level platform configuration aggregating all subsystem configs."""

    model_config = {"env_prefix": "ACI_"}

    # Deployment identification.
    tenant_id: str = Field(default="default", description="Customer tenant identifier")
    environment: str = Field(default="development", description="Deployment environment")
    runtime_role: str = Field(
        default="all",
        description="Runtime role (all|gateway|processor)",
    )

    # Subsystem configs.
    interceptor: InterceptorConfig = Field(default_factory=InterceptorConfig)
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)
    trac: TRACConfig = Field(default_factory=TRACConfig)
    carbon: CarbonConfig = Field(default_factory=CarbonConfig)
    equivalence: EquivalenceConfig = Field(default_factory=EquivalenceConfig)
    fbp: FBPConfig = Field(default_factory=FBPConfig)

    # Infrastructure endpoints.
    index_max_entries: int = Field(
        default=250_000,
        description="Max attribution index entries retained in memory",
    )
    index_backend: str = Field(
        default="memory",
        description="Attribution index backend (memory|redis)",
    )
    index_redis_prefix: str = Field(
        default="aci:index",
        description="Redis key prefix for durable index entries",
    )

    event_bus_backend: str = Field(
        default="memory",
        description="Event bus backend (memory|kafka)",
    )
    event_bus_topic: str = Field(default="aci.events", description="Primary event topic")
    event_bus_dlq_topic: str = Field(default="aci.events.dlq", description="Dead-letter topic")
    event_bus_consumer_group: str = Field(
        default="aci-processor",
        description="Consumer group for processor subscribers",
    )
    event_bus_dedup_ttl_s: int = Field(
        default=86_400,
        description="Idempotency dedup retention (seconds) for durable bus backends",
    )

    kafka_bootstrap: str = Field(default="localhost:9092", description="Kafka bootstrap servers")
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis URL")
    neo4j_uri: str = Field(default="bolt://localhost:7687", description="Neo4j connection URI")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="", description="Neo4j password")
