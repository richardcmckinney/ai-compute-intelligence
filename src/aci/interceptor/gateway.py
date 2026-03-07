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
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog
from prometheus_client import Counter, Gauge, Histogram

from aci.config import InterceptorConfig
from aci.interceptor.circuit_breaker import CircuitBreaker, CircuitStateStore
from aci.interceptor.shadow_warming import ShadowWarmer
from aci.models.carbon import EnforcementAction, PolicyEvaluationResult
from aci.models.events import DomainEvent, EventType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from aci.index.materializer import AttributionIndexStore
    from aci.models.attribution import AttributionIndexEntry

logger = structlog.get_logger()

FAIL_OPEN_TOTAL = Counter(
    "aci_fail_open_total",
    "Total fail-open outcomes by reason.",
    labelnames=("reason",),
)
FAIL_OPEN_RATE = Gauge(
    "aci_fail_open_rate",
    "Cumulative fail-open rate (total_fail_open / total_requests).",
)
INTERCEPTOR_LATENCY_P99 = Histogram(
    "aci_interceptor_latency_p99",
    "Interceptor latency observations (ms).",
    buckets=(1, 2, 5, 10, 15, 20, 30, 50, 75, 100, 200, 500),
)
CIRCUIT_BREAKER_STATE = Gauge(
    "aci_circuit_breaker_state",
    "Circuit breaker state encoded as closed=0, half_open=1, open=2.",
)


class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> bool | Coroutine[Any, Any, bool]: ...


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


class FailOpenReason(StrEnum):
    """Normalized fail-open reasons exposed in response headers."""

    INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
    POLICY_TIMEOUT = "POLICY_TIMEOUT"
    LOOKUP_TIMEOUT = "LOOKUP_TIMEOUT"
    DEPENDENCY_UNREACHABLE = "DEPENDENCY_UNREACHABLE"
    CONFIG_INVALID = "CONFIG_INVALID"
    RUNTIME_EXCEPTION = "RUNTIME_EXCEPTION"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"


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
    metadata: dict[str, Any] = field(default_factory=dict)
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
        event_bus: EventPublisher | None = None,
        shadow_refresh_fn: Callable[[str], Awaitable[None]] | None = None,
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
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def intercept(self, request: InterceptionRequest) -> InterceptionResult:
        """
        Process an inference request through the interception pipeline.

        Safety invariant: ALWAYS returns a result. NEVER raises an exception
        that would block the caller's inference request.
        """
        self.total_requests += 1
        start = time.monotonic()

        try:
            result = await self._intercept_inner(request, start)
            self._record_observability_metrics(result)
            return result
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
                    tenant_id=self._resolve_tenant_id(request),
                )
            logger.error(
                "interceptor.fail_open.exception",
                request_id=request.request_id,
                error=str(exc),
                elapsed_ms=elapsed_ms,
            )
            result = InterceptionResult(
                outcome=InterceptionOutcome.FAIL_OPEN,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
                shadow_event_logged=shadow_logged,
                enrichment_headers=self._build_fail_open_headers(FailOpenReason.RUNTIME_EXCEPTION),
            )
            self._record_observability_metrics(result)
            return result

    async def _intercept_inner(
        self,
        request: InterceptionRequest,
        start: float,
    ) -> InterceptionResult:
        """Core interception logic within the safety wrapper."""
        request.headers = self._sanitize_request_headers(request.headers)

        # Step 1: Circuit breaker.
        if self.circuit_breaker.is_open:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.total_fail_open += 1
            return InterceptionResult(
                outcome=InterceptionOutcome.CIRCUIT_OPEN,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
                enrichment_headers=self._build_fail_open_headers(FailOpenReason.RESOURCE_EXHAUSTION),
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
            if self._shadow_refresh_fn and self.shadow_warmer.should_trigger_refresh(workload_id):
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(
                        self.shadow_warmer.trigger_refresh(workload_id, self._shadow_refresh_fn)
                    )
                    self._track_background_task(task)
                except RuntimeError:
                    pass  # No running loop; skip background refresh.

            # Emit shadow event for async reconciliation (Section 6.3).
            shadow_logged = self._emit_shadow_event(
                EventType.SHADOW_INTERCEPT_MISS,
                workload_id,
                request.request_id,
                elapsed_ms,
                tenant_id=self._resolve_tenant_id(request),
            )

            return InterceptionResult(
                outcome=InterceptionOutcome.FAIL_OPEN,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
                shadow_event_logged=shadow_logged,
                enrichment_headers=self._build_fail_open_headers(FailOpenReason.INDEX_UNAVAILABLE),
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
                tenant_id=self._resolve_tenant_id(request),
            )
            return InterceptionResult(
                outcome=InterceptionOutcome.TIMEOUT,
                request_id=request.request_id,
                elapsed_ms=elapsed_so_far,
                attribution=attribution,
                enrichment_headers=self._build_basic_headers(
                    request,
                    attribution,
                    fail_open_reason=FailOpenReason.LOOKUP_TIMEOUT,
                ),
                shadow_event_logged=shadow_logged,
            )

        # Step 4: Policy evaluation. O(1) threshold checks.
        policy_start = time.monotonic()
        violations = self._evaluate_policies(request, attribution)
        policy_elapsed_ms = (time.monotonic() - policy_start) * 1000
        if policy_elapsed_ms > self.config.policy_timeout_ms:
            self.total_fail_open += 1
            elapsed_ms = (time.monotonic() - start) * 1000
            self._emit_policy_evaluation_event(
                request=request,
                workload_id=workload_id,
                violations=violations,
                elapsed_ms=elapsed_ms,
                fail_open=True,
            )
            shadow_logged = self._emit_shadow_event(
                EventType.SHADOW_INTERCEPT_TIMEOUT,
                workload_id,
                request.request_id,
                elapsed_ms,
                tenant_id=self._resolve_tenant_id(request),
            )
            return InterceptionResult(
                outcome=InterceptionOutcome.TIMEOUT,
                request_id=request.request_id,
                elapsed_ms=elapsed_ms,
                attribution=attribution,
                enrichment_headers=self._build_basic_headers(
                    request,
                    attribution,
                    fail_open_reason=FailOpenReason.POLICY_TIMEOUT,
                ),
                shadow_event_logged=shadow_logged,
            )
        self._emit_policy_evaluation_event(
            request=request,
            workload_id=workload_id,
            violations=violations,
            elapsed_ms=(time.monotonic() - start) * 1000,
            fail_open=False,
        )

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
            outcome = (
                InterceptionOutcome.SOFT_STOPPED if has_violation else InterceptionOutcome.ENRICHED
            )
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
            if (
                attribution.confidence < self.config.active_lite_min_confidence
                or self._should_downgrade_active_to_advisory(request)
            ):
                headers = self._build_enrichment_headers(request, attribution, violations)
                has_violation = any(v.violated for v in violations)
                outcome = (
                    InterceptionOutcome.SOFT_STOPPED
                    if has_violation
                    else InterceptionOutcome.ENRICHED
                )
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
            violations.append(
                PolicyEvaluationResult(
                    policy_id="model_allowlist",
                    policy_name="Production Model Allowlist",
                    action=EnforcementAction.SOFT_STOP,
                    violated=True,
                    details=f"Model '{request.model}' not in allowlist",
                )
            )

        # Budget ceiling (>90% utilized).
        if (
            attribution.budget_remaining_usd is not None
            and attribution.budget_limit_usd is not None
            and attribution.budget_limit_usd > 0
        ):
            utilization = 1.0 - (attribution.budget_remaining_usd / attribution.budget_limit_usd)
            if utilization > 0.9:
                violations.append(
                    PolicyEvaluationResult(
                        policy_id="budget_ceiling",
                        policy_name="Budget Ceiling",
                        action=EnforcementAction.SOFT_STOP,
                        violated=True,
                        details=f"Budget {utilization:.0%} utilized",
                    )
                )

        # Token budget.
        if (
            attribution.token_budget_output
            and request.metadata.get("max_tokens", 0) > attribution.token_budget_output
        ):
            violations.append(
                PolicyEvaluationResult(
                    policy_id="token_budget",
                    policy_name="Token Budget Per Request",
                    action=EnforcementAction.SOFT_STOP,
                    violated=True,
                    details=f"Requested tokens exceed budget of {attribution.token_budget_output}",
                )
            )

        # Input token budget (static payload size gate).
        if attribution.token_budget_input and request.input_tokens > attribution.token_budget_input:
            violations.append(
                PolicyEvaluationResult(
                    policy_id="token_budget_input",
                    policy_name="Token Size Limit",
                    action=EnforcementAction.SOFT_STOP,
                    violated=True,
                    details=(
                        f"Input tokens {request.input_tokens} exceed budget "
                        f"{attribution.token_budget_input}"
                    ),
                )
            )

        # Per-request cost ceiling.
        if (
            attribution.cost_ceiling_per_request_usd is not None
            and request.estimated_cost_usd > attribution.cost_ceiling_per_request_usd
        ):
            violations.append(
                PolicyEvaluationResult(
                    policy_id="cost_ceiling",
                    policy_name="High-Cost Model Approval Gate",
                    action=EnforcementAction.SOFT_STOP,
                    violated=True,
                    details=(
                        f"Estimated request cost {request.estimated_cost_usd:.6f} exceeds cap "
                        f"{attribution.cost_ceiling_per_request_usd:.6f}"
                    ),
                )
            )

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

        hard_stop_ids = {"model_allowlist", "budget_ceiling", "token_budget_input", "cost_ceiling"}
        hard_stops = [
            v
            for v in violations
            if v.action == EnforcementAction.HARD_STOP or v.policy_id in hard_stop_ids
        ]
        if hard_stops and attribution.confidence >= self.config.active_lite_gate_min_confidence:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._emit_intervention_event(
                request=request,
                action_type="GATE_REJECT",
                elapsed_ms=elapsed_ms,
                attribution=attribution,
                new_model=None,
            )
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
            headers["X-ACI-Intervention-Applied"] = "true"
            headers["X-ACI-Intervention-Type"] = "MODEL_ROUTE"
            headers["X-ACI-Intervention-Delta"] = (
                f"{{model:{request.model}->{alt_model}}}"
            )[:512]
            self._emit_intervention_event(
                request=request,
                action_type="MODEL_ROUTE",
                elapsed_ms=elapsed_ms,
                attribution=attribution,
                new_model=alt_model,
            )
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
        outcome = (
            InterceptionOutcome.SOFT_STOPPED if has_violation else InterceptionOutcome.ENRICHED
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        self.total_enriched += 1
        if has_violation:
            self._emit_intervention_event(
                request=request,
                action_type="SOFT_STOP",
                elapsed_ms=elapsed_ms,
                attribution=attribution,
                new_model=None,
            )
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
        fail_open_reason: FailOpenReason | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "X-ACI-Est-Cost-USD": f"{request.estimated_cost_usd:.6f}",
            "X-ACI-Attribution-Confidence": f"{attribution.confidence:.2f}",
        }
        snapshot_id = str(request.metadata.get("price_snapshot_id", "")).strip()
        if snapshot_id:
            headers["X-ACI-Price-Snapshot-Id"] = snapshot_id
        if fail_open_reason is not None:
            headers.update(self._build_fail_open_headers(fail_open_reason))
        return self._enforce_header_budget(headers)

    def _build_enrichment_headers(
        self,
        request: InterceptionRequest,
        attribution: AttributionIndexEntry,
        violations: list[PolicyEvaluationResult],
    ) -> dict[str, str]:
        """Full enrichment headers (Section 6.4)."""
        owner_id = attribution.team_id or attribution.person_id or attribution.team_name
        advisory_code, advisory_detail = self._build_advisory(
            request,
            attribution,
            violations,
        )

        headers: dict[str, str] = {
            "X-ACI-Est-Cost-USD": f"{request.estimated_cost_usd:.6f}",
            "X-ACI-Attribution-Owner-Id": owner_id,
            "X-ACI-Attribution-Confidence": f"{attribution.confidence:.2f}",
            "X-ACI-Attribution-Reason": self._map_attribution_reason(attribution.method_used),
            "X-ACI-Model": request.model,
            "X-ACI-Advisory": "true" if advisory_code else "false",
        }
        snapshot_id = str(request.metadata.get("price_snapshot_id", "")).strip()
        if snapshot_id:
            headers["X-ACI-Price-Snapshot-Id"] = snapshot_id
        if advisory_code:
            headers["X-ACI-Advisory-Code"] = advisory_code
            headers["X-ACI-Advisory-Detail"] = advisory_detail
        if attribution.team_name:
            headers["X-ACI-Team-Name"] = attribution.team_name

        if attribution.budget_remaining_usd is not None and attribution.budget_limit_usd:
            pct = (attribution.budget_remaining_usd / attribution.budget_limit_usd) * 100
            headers["X-ACI-Budget-Remaining-Pct"] = f"{pct:.0f}"

        if attribution.approved_alternatives:
            headers["X-ACI-Alternative-Models"] = ",".join(attribution.approved_alternatives)

        if attribution.confidence_tier == "provisional":
            headers["X-ACI-Confidence-Flag"] = "PROVISIONAL"
        elif attribution.confidence_tier == "estimated":
            headers["X-ACI-Confidence-Flag"] = "ESTIMATED"

        if violations:
            headers["X-ACI-Policy-Violations"] = str(len(violations))

        return self._enforce_header_budget(headers)

    def _enforce_header_budget(self, headers: dict[str, str]) -> dict[str, str]:
        """
        Enforce max header size budget and clamp oversized values.

        Reliability guardrail: avoid oversized headers that upstream gateways
        may reject or truncate unexpectedly.
        """
        clamped = {
            key: value[: self.config.max_header_value_length] for key, value in headers.items()
        }

        max_bytes = self.config.max_enrichment_header_bytes
        if self._estimate_header_bytes(clamped) <= max_bytes:
            return clamped

        # Keep core attribution headers; drop optional headers first.
        priority = [
            "X-ACI-Est-Cost-USD",
            "X-ACI-Attribution-Owner-Id",
            "X-ACI-Attribution-Confidence",
            "X-ACI-Attribution-Reason",
            "X-ACI-Price-Snapshot-Id",
            "X-ACI-Advisory",
            "X-ACI-Advisory-Code",
            "X-ACI-Advisory-Detail",
            "X-ACI-Confidence-Flag",
            "X-ACI-Policy-Violations",
            "X-ACI-Budget-Remaining-Pct",
            "X-ACI-Alternative-Models",
            "X-ACI-Intervention-Applied",
            "X-ACI-Intervention-Type",
            "X-ACI-Intervention-Delta",
            "X-ACI-Fail-Open",
            "X-ACI-Fail-Open-Reason",
        ]
        trimmed: dict[str, str] = {}
        for key in priority:
            if key in clamped:
                trimmed[key] = clamped[key]
            if self._estimate_header_bytes(trimmed) > max_bytes:
                trimmed.pop(key, None)
                break

        trimmed["X-ACI-Header-Budget-Applied"] = "1"
        return trimmed

    @staticmethod
    def _estimate_header_bytes(headers: dict[str, str]) -> int:
        return sum(len(key) + len(value) + 4 for key, value in headers.items())

    def _emit_shadow_event(
        self,
        event_type: EventType,
        workload_id: str,
        request_id: str,
        elapsed_ms: float,
        tenant_id: str = "",
    ) -> bool:
        """
        Emit a shadow event to the event bus for async reconciliation (Section 6.3).

        Fire-and-forget: this must never block the critical path. If the event
        bus is unavailable, the event is dropped and logged.
        """
        if not self.config.shadow_events_enabled:
            return False
        return self._publish_async_event(
            event_type=event_type,
            subject_id=workload_id,
            attributes={
                "request_id": request_id,
                "workload_id": workload_id,
                "elapsed_ms": elapsed_ms,
                "interceptor_mode": self.mode.value,
                "tenant_id": tenant_id,
            },
            idempotency_key=f"shadow:{request_id}",
            tenant_id=tenant_id,
        )

    def _emit_policy_evaluation_event(
        self,
        *,
        request: InterceptionRequest,
        workload_id: str,
        violations: list[PolicyEvaluationResult],
        elapsed_ms: float,
        fail_open: bool,
    ) -> bool:
        """Emit immutable audit event for every policy evaluation pass/fail."""
        tenant_id = self._resolve_tenant_id(request)
        violated_ids = [violation.policy_id for violation in violations if violation.violated]
        return self._publish_async_event(
            event_type=EventType.POLICY_EVALUATED,
            subject_id=workload_id,
            attributes={
                "request_id": request.request_id,
                "workload_id": workload_id,
                "violation_count": len(violated_ids),
                "violated_policy_ids": violated_ids,
                "mode": self.mode.value,
                "fail_open": fail_open,
                "elapsed_ms": elapsed_ms,
                "environment": str(request.metadata.get("environment", "")),
            },
            idempotency_key=f"policy_eval:{request.request_id}:{self.mode.value}",
            tenant_id=tenant_id,
        )

    def _emit_intervention_event(
        self,
        *,
        request: InterceptionRequest,
        action_type: str,
        elapsed_ms: float,
        attribution: AttributionIndexEntry,
        new_model: str | None,
    ) -> bool:
        """Emit immutable intervention audit event without blocking the hot path."""
        workload_id = self._resolve_workload_id(request)
        if not workload_id:
            return False
        tenant_id = self._resolve_tenant_id(request)
        return self._publish_async_event(
            event_type=EventType.INTERVENTION_APPLIED,
            subject_id=workload_id,
            attributes={
                "request_id": request.request_id,
                "workload_id": workload_id,
                "action_type": action_type,
                "original_model": request.model,
                "new_model": new_model or "",
                "attribution_confidence": attribution.confidence,
                "estimated_cost_usd": request.estimated_cost_usd,
                "environment": str(request.metadata.get("environment", "")),
                "elapsed_ms": elapsed_ms,
                "tenant_id": tenant_id,
            },
            idempotency_key=f"intervention:{request.request_id}:{action_type}",
            tenant_id=tenant_id,
        )

    def _publish_async_event(
        self,
        *,
        event_type: EventType,
        subject_id: str,
        attributes: dict[str, Any],
        idempotency_key: str,
        tenant_id: str = "",
    ) -> bool:
        """Best-effort async event publication for shadow/intervention events."""
        if self._event_bus is None:
            return False

        try:
            event = DomainEvent(
                event_type=event_type,
                subject_id=subject_id,
                attributes=attributes,
                event_time=datetime.now(UTC),
                source="interceptor",
                idempotency_key=idempotency_key,
                tenant_id=tenant_id,
            )
            publish_result = self._event_bus.publish(event)
            if inspect.isawaitable(publish_result):
                awaitable = cast("Awaitable[bool]", publish_result)
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    if inspect.iscoroutine(awaitable):
                        awaitable.close()
                    logger.debug("interceptor.event.no_running_loop", event_type=event_type.value)
                    return False

                task = loop.create_task(self._drain_shadow_publish(awaitable))
                self._track_background_task(task)
                return True

            return bool(publish_result)
        except Exception as exc:
            logger.debug(
                "interceptor.event.publish_failed",
                event_type=event_type.value,
                error=str(exc),
            )
            return False

    def _record_observability_metrics(self, result: InterceptionResult) -> None:
        """Update Prometheus metrics after interception outcome is computed."""
        INTERCEPTOR_LATENCY_P99.observe(result.elapsed_ms)

        fail_open_outcomes = {
            InterceptionOutcome.FAIL_OPEN,
            InterceptionOutcome.CIRCUIT_OPEN,
            InterceptionOutcome.TIMEOUT,
        }
        if result.outcome in fail_open_outcomes:
            reason = result.enrichment_headers.get("X-ACI-Fail-Open-Reason", result.outcome.value)
            FAIL_OPEN_TOTAL.labels(reason=reason).inc()

        if self.total_requests > 0:
            FAIL_OPEN_RATE.set(self.total_fail_open / self.total_requests)

        state_value = {
            "closed": 0.0,
            "half_open": 1.0,
            "open": 2.0,
        }.get(self.circuit_breaker.state, 0.0)
        CIRCUIT_BREAKER_STATE.set(state_value)

    @staticmethod
    def _build_fail_open_headers(reason: FailOpenReason) -> dict[str, str]:
        return {
            "X-ACI-Fail-Open": "true",
            "X-ACI-Fail-Open-Reason": reason.value,
        }

    @staticmethod
    def _map_attribution_reason(method_used: str) -> str:
        method = method_used.upper()
        if method.startswith("R1"):
            return "DIRECT_OWNER_MAPPING"
        if method.startswith("R2"):
            return "CI_PROVENANCE_MATCH"
        if method.startswith("R3") or method.startswith("R4") or method.startswith("R5"):
            return "HEURISTIC_MATCH"
        if method.startswith("R6"):
            return "GATEWAY_INHERITANCE"
        return "UNKNOWN"

    @staticmethod
    def _build_advisory(
        request: InterceptionRequest,
        attribution: AttributionIndexEntry,
        violations: list[PolicyEvaluationResult],
    ) -> tuple[str, str]:
        if (
            attribution.approved_alternatives
            and request.model not in attribution.approved_alternatives
        ):
            alt = attribution.approved_alternatives[0]
            detail = f"Equivalent lower-cost alternative available: {alt}"
            return ("SUGGEST_MODEL_DOWNGRADE", detail[:200])

        token_violation = next(
            (v for v in violations if v.policy_id in {"token_budget", "token_budget_input"}),
            None,
        )
        if token_violation is not None:
            return ("SUGGEST_TOKEN_CAP", token_violation.details[:200])

        cost_violation = next((v for v in violations if v.policy_id == "cost_ceiling"), None)
        if cost_violation is not None:
            return ("SUGGEST_COST_CAP", cost_violation.details[:200])

        return ("", "")

    @staticmethod
    def _should_downgrade_active_to_advisory(request: InterceptionRequest) -> bool:
        env = str(request.metadata.get("environment", "")).strip().lower()
        is_non_production = env in {"dev", "development", "test", "testing", "staging"}
        has_semantic_guardrail = (
            str(request.metadata.get("response_format_type", "")).strip().lower() == "json_object"
            or bool(request.metadata.get("tools_present", False))
            or bool(request.metadata.get("strict_json_schema", False))
        )

        if not is_non_production:
            return True
        return has_semantic_guardrail

    @staticmethod
    def _sanitize_request_headers(headers: dict[str, str]) -> dict[str, str]:
        sanitized: dict[str, str] = {}
        violation_detected = False
        for key, value in headers.items():
            header_name = key.lower()
            lower_value = value.lower()

            looks_like_email = "@" in value and "." in value
            looks_sensitive = (
                "api-key" in header_name
                or "authorization" in header_name
                or "prompt" in header_name
                or "token" in header_name
                or "bearer " in lower_value
                or looks_like_email
            )
            looks_prompt_like = len(value) > 1024 and (" " in value or "\n" in value)
            if looks_sensitive or looks_prompt_like:
                violation_detected = True
                continue
            sanitized[key] = value

        if violation_detected:
            logger.warning("WARN_HEADER_POLICY_VIOLATION")

        return sanitized

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

    @staticmethod
    def _resolve_tenant_id(request: InterceptionRequest) -> str:
        """Resolve tenant identifier for emitted audit/shadow events."""
        return str(request.metadata.get("tenant_id", "")).strip()

    async def _drain_shadow_publish(self, publish_result: Awaitable[bool]) -> None:
        """Best-effort async publish drain for fire-and-forget shadow events."""
        try:
            await publish_result
        except Exception as exc:
            logger.debug("interceptor.shadow_event.await_failed", error=str(exc))

    def _track_background_task(self, task: asyncio.Task[Any]) -> None:
        tracked = cast("asyncio.Task[None]", task)
        self._background_tasks.add(tracked)
        tracked.add_done_callback(self._background_tasks.discard)

    async def shutdown(self) -> None:
        """Drain outstanding background tasks during process shutdown."""
        if not self._background_tasks:
            return
        await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
        self._background_tasks.clear()

    def get_metrics(self) -> dict[str, int | str]:
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
    def stats(self) -> dict[str, int | str]:
        return self.get_metrics()
