"""
Fail-Open Decision-Time Interceptor (Patent Spec Section 6).

The interceptor operates on the critical path of AI inference requests.
It reads from the precomputed in-memory attribution index to enrich
requests with cost, attribution, and optimization signals. It NEVER
makes synchronous calls to the graph store, event bus, or any external
system. All decision-time operations execute in O(1) with respect to
attribution graph size.

Three deployment modes (Section 6.4):
- Passive Observation: log enrichment data, produce reports asynchronously.
- Advisory Mode: enrich API response headers, applications decide.
- Active Mode: may modify or redirect requests (requires opt-in).

Safety invariant: if ANY step exceeds its budget or fails, the original
request proceeds unmodified to the model provider.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable

import structlog

from aci.config import InterceptorConfig
from aci.index.materializer import AttributionIndexStore
from aci.interceptor.circuit_breaker import CircuitBreaker, CircuitStateStore
from aci.interceptor.shadow_warming import ShadowWarmer
from aci.models.attribution import AttributionIndexEntry
from aci.models.carbon import EnforcementAction, PolicyEvaluationResult
from aci.models.events import DomainEvent, EventType

logger = structlog.get_logger()


class DeploymentMode(StrEnum):
    """Interceptor deployment modes (Section 6.4)."""
    PASSIVE = "passive"
    ADVISORY = "advisory"
    ACTIVE = "active"


class InterceptionOutcome(StrEnum):
    """Possible outcomes of an interception decision."""
    PASSTHROUGH = "passthrough"
    ENRICHED = "enriched"
    REDIRECTED = "redirected"
    SOFT_STOPPED = "soft_stopped"
    HARD_STOPPED = "hard_stopped"
    FAIL_OPEN = "fail_open"
    CIRCUIT_OPEN = "circuit_open"
    TIMEOUT = "timeout"


@dataclass
class InterceptionRequest:
    """Incoming inference request to be intercepted."""
    request_id: str
    model: str
    provider: str = ""
    service_name: str = ""
    api_key_id: str = ""
    input_tokens: int = 0
    estimated_cost_usd: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    workload_id: str = ""
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class InterceptionResult:
    """Result of interceptor processing."""
    outcome: InterceptionOutcome
    request_id: str
    enrichment_headers: dict[str, str] = field(default_factory=dict)
    redirect_model: str | None = None
    policy_results: list[PolicyEvaluationResult] = field(default_factory=list)
    attribution: AttributionIndexEntry | None = None
    elapsed_ms: float = 0.0
    shadow_event_logged: bool = False


class FailOpenInterceptor:
    """
    The fail-open decision-time interceptor (Section 6).

    Pipeline per request:
    1. Check circuit breaker.
    2. O(1) index lookup (sync, in-memory).
    3. Policy evaluation (O(1) threshold checks from precomputed index).
    4. Routing decision (mode-dependent).
    5. Construct enrichment headers.

    Total budget: 20ms for enrichment, 50ms including routing.
    """

    def __init__(
        self,
        index: AttributionIndexStore,
        config: InterceptorConfig | None = None,
        mode: DeploymentMode = DeploymentMode.ADVISORY,
        event_bus: Any | None = None,
        shadow_refresh_fn: Callable | None = None,
        circuit_state_store: CircuitStateStore | None = None,
    ) -> None:
        self.index = index
        self.config = config or InterceptorConfig()
        self.mode = mode
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.config.circuit_breaker_threshold,
            reset_timeout_s=self.config.circuit_breaker_reset_s,
            state_store=circuit_state_store,
        )

        # Shadow warming (Section 6.3): probabilistic background refresh
        # on cache miss to prevent thundering herd.
        self.shadow_warmer = ShadowWarmer(self.config)
        self._shadow_refresh_fn = shadow_refresh_fn

        # Event bus for emitting shadow events on miss/timeout.
        # These events trigger async reconciliation for unknown workloads.
        self._event_bus = event_bus

        self.total_requests = 0
        self.total_enriched = 0
        self.total_fail_open = 0
        self.total_redirected = 0
        self.total_cache_misses = 0

    async def intercept(self, request: InterceptionRequest) -> InterceptionResult:
        """
        Process an inference request through the interception pipeline.

        Safety invariant: ALWAYS returns a result. NEVER raises an exception
        that would block the caller's inference request.
        """
        self.total_requests += 1
        start = time.monotonic()

        try:
            return self._intercept_inner(request, start)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.total_fail_open += 1
            self.circuit_breaker.record_failure()
            workload_id = self._resolve_workload_id(request)
            shadow_logged = False
            if workload_id:
                shadow_logged = self._emit_shadow_event(
                    EventType.SHADOW_INTERCEPT_TIMEOUT,
                    workload_id,
                    request.request_id,
                    elapsed_ms,
                )
            logger.error(
                "interceptor.fail_open.exception",
                request_id=request.request_id,
                error=str(exc),
                elapsed_ms=elapsed_ms,
            )
            return InterceptionResult(
                outcome=InterceptionOutcome.FAIL_OPEN,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
                shadow_event_logged=shadow_logged,
            )

    def _intercept_inner(
        self,
        request: InterceptionRequest,
        start: float,
    ) -> InterceptionResult:
        """Core interception logic within the safety wrapper."""

        # Step 1: Circuit breaker.
        if self.circuit_breaker.is_open:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.total_fail_open += 1
            return InterceptionResult(
                outcome=InterceptionOutcome.CIRCUIT_OPEN,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
            )

        # Step 2: O(1) index lookup. Synchronous hash map access.
        workload_id = self._resolve_workload_id(request)
        if not workload_id:
            elapsed_ms = (time.monotonic() - start) * 1000
            return InterceptionResult(
                outcome=InterceptionOutcome.PASSTHROUGH,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
            )

        attribution = self.index.lookup(workload_id)

        if attribution is None:
            self.total_cache_misses += 1
            self.total_fail_open += 1
            elapsed_ms = (time.monotonic() - start) * 1000

            # Shadow warming (Section 6.3): probabilistic background refresh.
            # Only one refresh runs at a time per workload ID.
            if (
                self._shadow_refresh_fn
                and self.shadow_warmer.should_trigger_refresh(workload_id)
            ):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self.shadow_warmer.trigger_refresh(workload_id, self._shadow_refresh_fn)
                    )
                except RuntimeError:
                    pass  # No running loop; skip background refresh.

            # Emit shadow event for async reconciliation (Section 6.3).
            shadow_logged = self._emit_shadow_event(
                EventType.SHADOW_INTERCEPT_MISS,
                workload_id,
                request.request_id,
                elapsed_ms,
            )

            return InterceptionResult(
                outcome=InterceptionOutcome.FAIL_OPEN,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
                shadow_event_logged=shadow_logged,
            )

        # Step 3: Time budget check.
        elapsed_so_far = (time.monotonic() - start) * 1000
        if self.config.timeout_ms - elapsed_so_far <= 2.0:
            self.total_fail_open += 1
            shadow_logged = self._emit_shadow_event(
                EventType.SHADOW_INTERCEPT_TIMEOUT,
                workload_id,
                request.request_id,
                elapsed_so_far,
            )
            return InterceptionResult(
                outcome=InterceptionOutcome.TIMEOUT,
                request_id=request.request_id,
                elapsed_ms=elapsed_so_far,
                attribution=attribution,
                enrichment_headers=self._build_basic_headers(request, attribution),
                shadow_event_logged=shadow_logged,
            )

        # Step 4: Policy evaluation. O(1) threshold checks.
        violations = self._evaluate_policies(request, attribution)

        # Step 5: Mode-dependent routing.
        if self.mode == DeploymentMode.PASSIVE:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.circuit_breaker.record_success()
            self.total_enriched += 1
            return InterceptionResult(
                outcome=InterceptionOutcome.PASSTHROUGH,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
                attribution=attribution,
                policy_results=violations,
                shadow_event_logged=False,
            )

        elif self.mode == DeploymentMode.ADVISORY:
            headers = self._build_enrichment_headers(request, attribution, violations)
            has_violation = any(v.violated for v in violations)
            outcome = InterceptionOutcome.SOFT_STOPPED if has_violation else InterceptionOutcome.ENRICHED
            elapsed_ms = (time.monotonic() - start) * 1000
            self.circuit_breaker.record_success()
            self.total_enriched += 1
            return InterceptionResult(
                outcome=outcome,
                request_id=request.request_id,
                enrichment_headers=headers,
                elapsed_ms=elapsed_ms,
                attribution=attribution,
                policy_results=violations,
            )

        elif self.mode == DeploymentMode.ACTIVE:
            result = self._active_routing(request, attribution, violations, start)
            self.circuit_breaker.record_success()
            return result

        elapsed_ms = (time.monotonic() - start) * 1000
        return InterceptionResult(
            outcome=InterceptionOutcome.PASSTHROUGH,
            request_id=request.request_id,
            elapsed_ms=elapsed_ms,
        )

    def _evaluate_policies(
        self,
        request: InterceptionRequest,
        attribution: AttributionIndexEntry,
    ) -> list[PolicyEvaluationResult]:
        """Pre-computed policy constraints. O(1) threshold checks only."""
        violations: list[PolicyEvaluationResult] = []

        # Model allowlist.
        if attribution.model_allowlist and request.model not in attribution.model_allowlist:
            violations.append(PolicyEvaluationResult(
                policy_id="model_allowlist",
                policy_name="Production Model Allowlist",
                action=EnforcementAction.SOFT_STOP,
                violated=True,
                details=f"Model '{request.model}' not in allowlist",
            ))

        # Budget ceiling (>90% utilized).
        if (
            attribution.budget_remaining_usd is not None
            and attribution.budget_limit_usd is not None
            and attribution.budget_limit_usd > 0
        ):
            utilization = 1.0 - (attribution.budget_remaining_usd / attribution.budget_limit_usd)
            if utilization > 0.9:
                violations.append(PolicyEvaluationResult(
                    policy_id="budget_ceiling",
                    policy_name="Budget Ceiling",
                    action=EnforcementAction.SOFT_STOP,
                    violated=True,
                    details=f"Budget {utilization:.0%} utilized",
                ))

        # Token budget.
        if (
            attribution.token_budget_output
            and request.metadata.get("max_tokens", 0) > attribution.token_budget_output
        ):
            violations.append(PolicyEvaluationResult(
                policy_id="token_budget",
                policy_name="Token Budget Per Request",
                action=EnforcementAction.SOFT_STOP,
                violated=True,
                details=f"Requested tokens exceed budget of {attribution.token_budget_output}",
            ))

        return violations

    def _active_routing(
        self,
        request: InterceptionRequest,
        attribution: AttributionIndexEntry,
        violations: list[PolicyEvaluationResult],
        start: float,
    ) -> InterceptionResult:
        """Active mode: may redirect to equivalent model (Section 6.2)."""
        headers = self._build_enrichment_headers(request, attribution, violations)

        hard_stops = [v for v in violations if v.action == EnforcementAction.HARD_STOP]
        if hard_stops:
            elapsed_ms = (time.monotonic() - start) * 1000
            return InterceptionResult(
                outcome=InterceptionOutcome.HARD_STOPPED,
                request_id=request.request_id,
                enrichment_headers=headers,
                elapsed_ms=elapsed_ms,
                attribution=attribution,
                policy_results=violations,
            )

        if (
            attribution.approved_alternatives
            and request.model not in attribution.approved_alternatives
        ):
            alt_model = attribution.approved_alternatives[0]
            elapsed_ms = (time.monotonic() - start) * 1000
            self.total_redirected += 1
            headers["X-Original-Model"] = request.model
            headers["X-Redirected-To"] = alt_model
            return InterceptionResult(
                outcome=InterceptionOutcome.REDIRECTED,
                request_id=request.request_id,
                enrichment_headers=headers,
                redirect_model=alt_model,
                elapsed_ms=elapsed_ms,
                attribution=attribution,
                policy_results=violations,
            )

        has_violation = any(v.violated for v in violations)
        outcome = InterceptionOutcome.SOFT_STOPPED if has_violation else InterceptionOutcome.ENRICHED
        elapsed_ms = (time.monotonic() - start) * 1000
        self.total_enriched += 1
        return InterceptionResult(
            outcome=outcome,
            request_id=request.request_id,
            enrichment_headers=headers,
            elapsed_ms=elapsed_ms,
            attribution=attribution,
            policy_results=violations,
        )

    def _build_basic_headers(
        self,
        request: InterceptionRequest,
        attribution: AttributionIndexEntry,
    ) -> dict[str, str]:
        return {
            "X-Compute-Cost-USD": f"{request.estimated_cost_usd:.6f}",
            "X-Attribution-Confidence": f"{attribution.confidence:.2f}",
        }

    def _build_enrichment_headers(
        self,
        request: InterceptionRequest,
        attribution: AttributionIndexEntry,
        violations: list[PolicyEvaluationResult],
    ) -> dict[str, str]:
        """Full enrichment headers (Section 6.4)."""
        headers: dict[str, str] = {
            "X-Compute-Cost-USD": f"{request.estimated_cost_usd:.6f}",
            "X-Attribution-Confidence": f"{attribution.confidence:.2f}",
            "X-Attribution-Team": attribution.team_name,
            "X-Attribution-Method": attribution.method_used,
        }

        if attribution.budget_remaining_usd is not None and attribution.budget_limit_usd:
            pct = (attribution.budget_remaining_usd / attribution.budget_limit_usd) * 100
            headers["X-Budget-Remaining-Pct"] = f"{pct:.0f}"

        if attribution.approved_alternatives:
            headers["X-Alternative-Models-Available"] = ",".join(attribution.approved_alternatives)

        if attribution.confidence_tier == "provisional":
            headers["X-Confidence-Flag"] = "PROVISIONAL"
        elif attribution.confidence_tier == "estimated":
            headers["X-Confidence-Flag"] = "ESTIMATED"

        if violations:
            headers["X-Policy-Violations"] = str(len(violations))

        return headers

    def _emit_shadow_event(
        self,
        event_type: EventType,
        workload_id: str,
        request_id: str,
        elapsed_ms: float,
    ) -> bool:
        """
        Emit a shadow event to the event bus for async reconciliation (Section 6.3).

        Fire-and-forget: this must never block the critical path. If the event
        bus is unavailable, the event is dropped and logged.
        """
        if not self.config.shadow_events_enabled:
            return False
        if self._event_bus is None:
            return True

        try:
            event = DomainEvent(
                event_type=event_type,
                subject_id=workload_id,
                attributes={
                    "request_id": request_id,
                    "workload_id": workload_id,
                    "elapsed_ms": elapsed_ms,
                    "interceptor_mode": self.mode.value,
                },
                event_time=datetime.now(timezone.utc),
                source="interceptor",
                idempotency_key=f"shadow:{request_id}",
                tenant_id="",  # Populated by bus middleware in production.
            )
            publish_result = self._event_bus.publish(event)
            if inspect.isawaitable(publish_result):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(publish_result)
                except RuntimeError:
                    asyncio.run(publish_result)
                return True

            return bool(publish_result)
        except Exception as exc:
            # Shadow event emission must never raise on the hot path.
            logger.debug("interceptor.shadow_event.failed", error=str(exc))
            return False

    @staticmethod
    def _resolve_workload_id(request: InterceptionRequest) -> str:
        """Choose the best identifier available for index lookup."""
        candidates = (
            request.workload_id,
            request.service_name,
            request.api_key_id,
            str(request.metadata.get("workload_id", "")),
        )
        for candidate in candidates:
            normalized = candidate.strip()
            if normalized:
                return normalized
        return ""

    def get_metrics(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "total_enriched": self.total_enriched,
            "total_fail_open": self.total_fail_open,
            "total_redirected": self.total_redirected,
            "total_cache_misses": self.total_cache_misses,
            "mode": self.mode.value,
            "circuit_breaker_state": self.circuit_breaker.state,
        }

    @property
    def stats(self) -> dict:
        return self.get_metrics()
