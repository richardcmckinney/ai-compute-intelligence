"""
FastAPI application: API surface for the ACI platform.

Routes serve attribution data, policy management, benchmarks, and health checks.
The API runs in the vendor control plane (SaaS) or within customer VPC depending
on deployment topology.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from jwt import InvalidTokenError
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from aci.api.auth import (
    can_bypass_auth,
    decode_and_validate_token,
    is_auth_required,
    is_public_path,
)
from aci.api.runtime import AppState, SlidingWindowRateLimiter, ensure_app_state
from aci.core.event_schema import EventSchemaValidationError, validate_event_attributes
from aci.demo.seeder import bootstrap_demo_state
from aci.integrations.notifications import NotificationMessage
from aci.interceptor.gateway import (
    DeploymentMode,
    InterceptionOutcome,
    InterceptionRequest,
    InterceptionResult,
)
from aci.models.events import DomainEvent, EventType
from aci.pricing.catalog import PricingUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from aci.interventions.registry import InterventionRecord

logger = structlog.get_logger()
__all__ = ["AppState", "SlidingWindowRateLimiter", "app", "get_state"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialize and tear down platform components."""
    app_state = get_state(app)
    logger.info("platform.starting", tenant=app_state.config.tenant_id)
    await app_state.start()
    yield
    await app_state.stop()
    logger.info("platform.shutting_down")


app = FastAPI(
    title="ACI Platform",
    description="AI Compute Intelligence: application-level cost and carbon attribution",
    version="0.2.0",
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=500)


class _RequestBodyTooLargeError(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized ingestion payloads before FastAPI parses the request body."""

    _LIMITED_PATHS = {"/v1/events/ingest", "/v1/events/ingest/batch"}

    def __init__(self, app: ASGIApp, default_max_body_bytes: int = 1_048_576) -> None:
        self.app = app
        self.default_max_body_bytes = default_max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")
        if method != "POST" or path not in self._LIMITED_PATHS:
            await self.app(scope, receive, send)
            return

        max_body_bytes = self._resolve_limit(scope)
        content_length = self._content_length(scope)
        if content_length is not None and content_length > max_body_bytes:
            await self._send_too_large(send, max_body_bytes)
            return

        total_body_bytes = 0

        async def limited_receive() -> Message:
            nonlocal total_body_bytes
            message = await receive()
            if message["type"] == "http.request":
                total_body_bytes += len(message.get("body", b""))
                if total_body_bytes > max_body_bytes:
                    raise _RequestBodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLargeError:
            await self._send_too_large(send, max_body_bytes)

    def _resolve_limit(self, scope: Scope) -> int:
        app_instance = scope.get("app")
        if isinstance(app_instance, FastAPI):
            return get_state(app_instance).config.api_max_request_bytes
        return self.default_max_body_bytes

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    return int(value.decode("latin-1"))
                except ValueError:
                    return None
        return None

    @staticmethod
    async def _send_too_large(send: Send, max_body_bytes: int) -> None:
        body = (
            f'{{"detail":"request body exceeds configured max {max_body_bytes} bytes"}}'
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


app.add_middleware(
    RequestBodyLimitMiddleware,
    default_max_body_bytes=int(os.getenv("ACI_API_MAX_REQUEST_BYTES", "1048576")),
)


def get_state(app_instance: FastAPI | None = None) -> AppState:
    """Return state attached to the FastAPI application instance."""
    return ensure_app_state(app if app_instance is None else app_instance)


cors_allowed_origins = [
    origin.strip()
    for origin in os.getenv("ACI_API_CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allowed_origins,
        allow_credentials=os.getenv("ACI_API_CORS_ALLOW_CREDENTIALS", "false").lower() == "true",
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With", "X-ACI-Request-ID"],
    )


@app.middleware("http")
async def add_security_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Apply baseline security headers to all HTTP responses."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.middleware("http")
async def enforce_service_auth(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Enforce bearer-token authentication for private API endpoints."""
    path = request.url.path
    if is_public_path(path) or not is_auth_required(path):
        return await call_next(request)

    app_state = get_state(request.app)
    auth_config = app_state.config.auth
    if not auth_config.enabled:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        if can_bypass_auth(app_state.config):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "missing bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header.removeprefix("Bearer ").strip()
    try:
        claims = decode_and_validate_token(token, auth_config, app_state.config.tenant_id)
    except InvalidTokenError as exc:
        logger.warning("auth.token_invalid", error=str(exc), path=path)
        return JSONResponse(
            status_code=401,
            content={"detail": "invalid bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.auth_claims = claims
    return await call_next(request)


@app.middleware("http")
async def enforce_runtime_surface(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Restrict role-specific HTTP surface area to the routes a pod should serve."""
    path = request.url.path
    always_available_paths = {"/", "/health", "/live", "/ready", "/metrics", "/metrics/prometheus"}
    if path in always_available_paths or path.startswith("/platform"):
        return await call_next(request)

    app_state = get_state(request.app)
    if app_state.runtime_role == "gateway" and path.startswith("/v1/") and path != "/v1/intercept":
        return JSONResponse(
            status_code=503,
            content={"detail": "route unavailable for runtime role 'gateway'"},
        )

    return await call_next(request)


# Serve improved platform mockup from the repo.
frontend_dir = Path(__file__).resolve().parents[3] / "frontend"
if frontend_dir.exists():
    app.mount("/platform", StaticFiles(directory=str(frontend_dir), html=True), name="platform")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RootResponse(BaseModel):
    name: str
    version: str
    health_url: str
    mockup_url: str | None


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    index_size: int
    runtime_role: str
    interceptor_mode: str
    circuit_breaker: str


class LivenessResponse(BaseModel):
    status: str
    timestamp: str


class ReadinessResponse(BaseModel):
    status: str
    timestamp: str
    checks: dict[str, bool]


class InterceptRequest(BaseModel):
    request_id: str
    model: str
    provider: str = ""
    service_name: str = ""
    workload_id: str = ""
    api_key_id: str = ""
    input_tokens: int = 0
    max_tokens: int = 0
    estimated_cost_usd: float = 0.0
    environment: str = ""
    route: str = ""
    response_format_type: str = ""
    tools_present: bool = False
    strict_json_schema: bool = False


class InterceptResponse(BaseModel):
    request_id: str
    outcome: str
    elapsed_ms: float
    enrichment_headers: dict[str, str]
    redirect_model: str | None = None
    attribution_team: str | None = None
    attribution_confidence: float | None = None


class PolicySignalsRequest(BaseModel):
    est_cost_usd: float = 0.0


class ServicePolicyRequest(BaseModel):
    active_lite_enabled: bool = False
    allowed_actions: list[str] = Field(default_factory=list)
    min_route_confidence: float = 0.90
    max_tokens_cap: int | None = None


class PolicyEvaluateRequest(BaseModel):
    request_id: str
    env: str
    service_id: str
    model_requested: str
    attribution: dict[str, Any] = Field(default_factory=dict)
    signals: PolicySignalsRequest = Field(default_factory=PolicySignalsRequest)
    mode: str = "ADVISORY"
    service_policy: ServicePolicyRequest = Field(default_factory=ServicePolicyRequest)


class PolicyActionResponse(BaseModel):
    type: str
    confidence: float
    delta: str


class PolicyAdvisoryResponse(BaseModel):
    code: str
    detail: str


class PolicyEvaluateResponse(BaseModel):
    decision: str
    advisories: list[PolicyAdvisoryResponse]
    actions: list[PolicyActionResponse]
    fail_open: bool


class TRACRequest(BaseModel):
    workload_id: str
    billed_cost_usd: float
    emissions_kg_co2e: float = 0.0
    attribution_confidence: float = 0.95
    signal_age_days: float = 0.0


class TRACResponse(BaseModel):
    workload_id: str
    trac_usd: float
    billed_cost_usd: float
    carbon_liability_usd: float
    confidence_risk_premium_usd: float
    carbon_pct_of_trac: float
    risk_pct_of_trac: float


class IndexLookupResponse(BaseModel):
    workload_id: str
    team_id: str
    team_name: str
    cost_center_id: str
    confidence: float
    confidence_tier: str
    method_used: str
    version: int


class IndexConstraintResponse(BaseModel):
    type: str
    scope: str


class IndexLookupAttributionResponse(BaseModel):
    owner_id: str | None
    team_id: str | None
    confidence: float
    reason: str
    explanation_id: str


class IndexLookupByKeyResponse(BaseModel):
    key: str
    index_version: int
    attribution: IndexLookupAttributionResponse
    constraints: list[IndexConstraintResponse]
    cache_ttl_ms: int


class EventIngestRequest(BaseModel):
    event_type: EventType
    subject_id: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    event_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    idempotency_key: str


class EventIngestResponse(BaseModel):
    accepted: bool
    event_id: str


class EventBatchIngestRequest(BaseModel):
    events: list[EventIngestRequest]


class EventBatchIngestResponse(BaseModel):
    total: int
    accepted: int
    deduplicated: int


class DashboardOverviewResponse(BaseModel):
    tenant_id: str
    environment: str
    runtime_role: str
    generated_at: str
    index_size: int
    index_hit_rate: float
    interceptor_requests: int
    interceptor_enriched: int
    interceptor_fail_open: int
    interceptor_cache_misses: int
    interceptor_mode: str
    circuit_breaker_state: str
    events_published: int


class DemoBootstrapResponse(BaseModel):
    seeded_entries: int
    workloads: list[str]
    events_published: int
    graph_nodes: int
    graph_edges: int
    interceptor_mode: str
    environment: str


class DemoModeRequest(BaseModel):
    mode: Literal["passive", "advisory", "active"]


class DemoModeResponse(BaseModel):
    interceptor_mode: str
    environment: str


class PricingRuleResponse(BaseModel):
    provider: str
    model: str
    effective_from: str
    input_usd_per_1k_tokens: float
    output_usd_per_1k_tokens: float
    cached_input_usd_per_1k_tokens: float | None
    request_base_usd: float


class PricingEstimateRequest(BaseModel):
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    request_count: int = 1
    effective_at: datetime | None = None


class PricingEstimateResponse(BaseModel):
    provider: str
    model: str
    request_count: int
    rule_effective_from: str
    input_cost_usd: float
    cached_input_cost_usd: float
    output_cost_usd: float
    request_base_cost_usd: float
    total_cost_usd: float


class SyntheticCostIngestRequest(BaseModel):
    request_id: str
    service_name: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    synthetic_cost_usd: float | None = None
    recorded_at: datetime | None = None


class SyntheticCostIngestResponse(BaseModel):
    request_id: str
    synthetic_cost_usd: float
    provider: str
    model: str
    recorded_at: str
    reconciled: bool


class CostReconcileRequest(BaseModel):
    request_id: str
    reconciled_cost_usd: float
    source: str = "billing_api"
    recorded_at: datetime | None = None


class CostReconcileResponse(BaseModel):
    request_id: str
    synthetic_cost_usd: float
    reconciled_cost_usd: float
    drift_usd: float
    drift_pct: float
    source: str


class DriftSummaryResponse(BaseModel):
    group: str
    total_records: int
    reconciled_records: int
    unresolved_records: int
    synthetic_cost_usd: float
    reconciled_cost_usd: float
    absolute_drift_usd: float
    drift_pct: float
    window: Literal["daily", "weekly"]
    threshold_pct: float
    investigation_required: bool
    dashboard_annotation: str


class SpendForecastRequest(BaseModel):
    monthly_spend_usd: list[float]
    horizon_months: int = 3


class SpendForecastPointResponse(BaseModel):
    month_offset: int
    predicted_spend_usd: float
    lower_bound_usd: float
    upper_bound_usd: float


class SpendForecastResponse(BaseModel):
    trend_pct: float
    residual_stddev_usd: float
    points: list[SpendForecastPointResponse]


class CostSimulationRequest(BaseModel):
    service_id: str
    provider: str
    current_model: str
    avg_input_tokens: int
    avg_output_tokens: int
    requests_per_day: int
    candidate_models: list[str] = Field(default_factory=list)


class CostSimulationCandidateResponse(BaseModel):
    model: str
    projected_monthly_cost_usd: float
    monthly_savings_usd: float
    savings_pct: float


class CostSimulationResponse(BaseModel):
    service_id: str
    provider: str
    current_model: str
    projected_monthly_cost_usd: float
    candidates: list[CostSimulationCandidateResponse]


class InterventionMethodologyResponse(BaseModel):
    category: str
    threshold_condition: str
    rule_condition: str


class InterventionResponse(BaseModel):
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
    status: str
    methodology: InterventionMethodologyResponse
    updated_at: str
    updated_by: str
    update_note: str


class InterventionSummaryResponse(BaseModel):
    total_count: int
    recommended_count: int
    review_count: int
    approved_count: int
    implemented_count: int
    dismissed_count: int
    total_potential_usd: float
    captured_savings_usd: float
    open_potential_usd: float


class InterventionListResponse(BaseModel):
    summary: InterventionSummaryResponse
    interventions: list[InterventionResponse]


class InterventionTransitionRequest(BaseModel):
    status: Literal["recommended", "review", "approved", "implemented", "dismissed"]
    actor: str = "api"
    note: str = ""


class NotificationRequest(BaseModel):
    event_type: str
    title: str
    detail: str
    severity: str = "info"
    channels: list[str]
    slack_webhook_url: str = ""
    webhook_url: str = ""
    email_to: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class NotificationDeliveryResponse(BaseModel):
    delivery_id: str
    channel: str
    target: str
    status: str
    message: str
    sent_at: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _ingest_rate_limit_key(request: Request) -> str:
    """Derive a stable per-caller key for ingestion throttling."""
    app_state = get_state(request.app)
    claims = getattr(request.state, "auth_claims", None)
    if isinstance(claims, dict):
        subject = str(claims.get("sub", "")).strip()
        if subject:
            return f"sub:{subject}"

    forwarded_for = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if forwarded_for and _request_came_from_trusted_proxy(request, app_state):
        return f"ip:{forwarded_for}"

    if request.client and request.client.host:
        return f"ip:{request.client.host}"

    return "anonymous"


def _request_came_from_trusted_proxy(request: Request, app_state: AppState) -> bool:
    trusted_entries = [
        item.strip()
        for item in app_state.config.api_trusted_proxy_cidrs.split(",")
        if item.strip()
    ]
    if not trusted_entries or request.client is None:
        return False

    try:
        client_ip = ip_address(request.client.host)
    except ValueError:
        return False

    for entry in trusted_entries:
        try:
            if client_ip in ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.warning("proxy_config.invalid_cidr", cidr=entry)
    return False


def _extract_aci_headers(request: Request) -> dict[str, str]:
    """Extract ACI namespaced headers from incoming HTTP request."""
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower().startswith("x-aci-")
    }


def _normalize_route(route: str) -> str:
    """
    Normalize route strings to bounded template-like keys.

    Raw URLs, query strings, and fragments are collapsed to the logical route
    shape to keep route-cardinality bounded.
    """
    normalized = route.strip()
    if not normalized:
        return ""

    if "://" in normalized:
        normalized = normalized.split("://", 1)[1]
        normalized = "/" + normalized.split("/", 1)[1] if "/" in normalized else "/"

    normalized = normalized.split("?", 1)[0]
    normalized = normalized.split("#", 1)[0]
    return normalized[:128]


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


def _is_production_like(environment: str) -> bool:
    return environment.strip().lower() in {"production", "staging"}


def _to_intervention_response(record: InterventionRecord) -> InterventionResponse:
    """Serialize registry record into API response model."""
    return InterventionResponse(
        intervention_id=record.intervention_id,
        title=record.title,
        intervention_type=record.intervention_type,
        team=record.team,
        detail=record.detail,
        savings_usd_month=record.savings_usd_month,
        confidence_pct=record.confidence_pct,
        risk=record.risk,
        equivalence_mode=record.equivalence_mode,
        equivalence_status=record.equivalence_status,
        status=record.status,
        methodology=InterventionMethodologyResponse(
            category=record.category,
            threshold_condition=record.threshold_condition,
            rule_condition=record.rule_condition,
        ),
        updated_at=record.updated_at.isoformat(),
        updated_by=record.updated_by,
        update_note=record.update_note,
    )


def _gate_error_from_result(result: InterceptionResult) -> JSONResponse | None:
    """Map active gate policy violations to explicit HTTP error responses."""
    hard_violations = [
        violation
        for violation in result.policy_results
        if violation.violated
        and violation.policy_id
        in {"token_budget_input", "model_allowlist", "budget_ceiling", "cost_ceiling"}
    ]
    if not hard_violations:
        return None

    primary = hard_violations[0]
    if primary.policy_id == "token_budget_input":
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "type": "token_size_exceeded",
                    "message": primary.details,
                    "request_id": result.request_id,
                    "policy_id": primary.policy_id,
                    "retry": False,
                }
            },
        )
    if primary.policy_id == "model_allowlist":
        approved = result.attribution.approved_alternatives if result.attribution else []
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "type": "model_not_allowed",
                    "message": primary.details,
                    "request_id": result.request_id,
                    "policy_id": primary.policy_id,
                    "approved_alternatives": approved,
                }
            },
        )
    if primary.policy_id == "budget_ceiling":
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "type": "budget_exceeded",
                    "message": primary.details,
                    "request_id": result.request_id,
                    "policy_id": primary.policy_id,
                    "retry_after_seconds": 3600,
                }
            },
        )
    if primary.policy_id == "cost_ceiling":
        approved = result.attribution.approved_alternatives if result.attribution else []
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "type": "cost_approval_required",
                    "message": primary.details,
                    "request_id": result.request_id,
                    "policy_id": primary.policy_id,
                    "approved_alternatives": approved,
                }
            },
        )
    return None


@app.get("/", response_model=RootResponse)
async def root() -> RootResponse:
    """Root metadata and quick links."""
    mockup_url = "/platform/" if frontend_dir.exists() else None
    return RootResponse(
        name="ACI Platform",
        version="0.2.0",
        health_url="/health",
        mockup_url=mockup_url,
    )


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Platform summary health check for operators."""
    app_state = get_state()
    checks = app_state.readiness_checks()
    status = "healthy" if all(checks.values()) else "degraded"
    return HealthResponse(
        status=status,
        timestamp=datetime.now(UTC).isoformat(),
        index_size=app_state.index_store.size,
        runtime_role=app_state.runtime_role,
        interceptor_mode=app_state.interceptor.mode.value,
        circuit_breaker=app_state.interceptor.circuit_breaker.state,
    )


@app.get("/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    """Liveness probe endpoint: process is up and serving requests."""
    return LivenessResponse(
        status="alive",
        timestamp=datetime.now(UTC).isoformat(),
    )


@app.get("/ready", response_model=ReadinessResponse)
async def ready() -> ReadinessResponse:
    """Readiness probe endpoint: required runtime dependencies are available."""
    checks = get_state().readiness_checks()
    if not all(checks.values()):
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "checks": checks,
            },
        )

    return ReadinessResponse(
        status="ready",
        timestamp=datetime.now(UTC).isoformat(),
        checks=checks,
    )


@app.get("/metrics")
async def metrics() -> dict[str, dict[str, Any]]:
    """Platform metrics for monitoring."""
    app_state = get_state()
    return {
        "index": app_state.index_store.stats,
        "interceptor": app_state.interceptor.stats,
        "processor": app_state.processor.stats,
        "event_bus": app_state.event_bus.stats,
    }


@app.get("/metrics/prometheus")
async def metrics_prometheus() -> Response:
    """Prometheus exposition format metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/events/ingest", response_model=EventIngestResponse)
async def ingest_event(request: Request, req: EventIngestRequest) -> EventIngestResponse:
    """Ingest a single domain event into the append-only bus."""
    app_state = get_state()
    if not app_state.accepts_ingestion:
        raise HTTPException(
            status_code=503, detail="event ingestion disabled for this runtime role"
        )
    if not await app_state.ingest_rate_limiter.allow(_ingest_rate_limit_key(request)):
        raise HTTPException(status_code=429, detail="ingestion rate limit exceeded")

    try:
        attributes = validate_event_attributes(req.event_type, req.attributes)
    except EventSchemaValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    event = DomainEvent(
        event_type=req.event_type,
        subject_id=req.subject_id,
        attributes=attributes,
        event_time=req.event_time,
        source=req.source,
        idempotency_key=req.idempotency_key,
        tenant_id=app_state.config.tenant_id,
    )
    event_bus = cast("Any", app_state.event_bus)
    accepted = await event_bus.publish(event)
    return EventIngestResponse(accepted=accepted, event_id=event.event_id)


@app.post("/v1/events/ingest/batch", response_model=EventBatchIngestResponse)
async def ingest_events_batch(
    request: Request,
    req: EventBatchIngestRequest,
) -> EventBatchIngestResponse:
    """Ingest a batch of domain events with idempotency-aware accounting."""
    app_state = get_state()
    if not app_state.accepts_ingestion:
        raise HTTPException(
            status_code=503, detail="event ingestion disabled for this runtime role"
        )
    if not await app_state.ingest_rate_limiter.allow(_ingest_rate_limit_key(request)):
        raise HTTPException(status_code=429, detail="ingestion rate limit exceeded")
    if len(req.events) > app_state.ingest_max_batch_size:
        raise HTTPException(
            status_code=413,
            detail=(
                f"batch size {len(req.events)} exceeds configured max "
                f"{app_state.ingest_max_batch_size}"
            ),
        )

    events: list[DomainEvent] = []
    for e in req.events:
        try:
            attrs = validate_event_attributes(e.event_type, e.attributes)
        except EventSchemaValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        events.append(
            DomainEvent(
                event_type=e.event_type,
                subject_id=e.subject_id,
                attributes=attrs,
                event_time=e.event_time,
                source=e.source,
                idempotency_key=e.idempotency_key,
                tenant_id=app_state.config.tenant_id,
            )
        )
    event_bus = cast("Any", app_state.event_bus)
    result = await event_bus.publish_batch(events)
    return EventBatchIngestResponse(
        total=len(events),
        accepted=result["published"],
        deduplicated=result["deduplicated"],
    )


@app.post("/v1/demo/bootstrap", response_model=DemoBootstrapResponse)
async def bootstrap_demo() -> DemoBootstrapResponse:
    """
    Seed deterministic demo index entries for reviewer walkthroughs.

    This endpoint is intentionally disabled in production/staging.
    """
    app_state = get_state()
    if _is_production_like(app_state.config.environment):
        raise HTTPException(
            status_code=403,
            detail="demo bootstrap endpoint is disabled in production/staging",
        )

    result = await bootstrap_demo_state(app_state, reset_existing=True)

    return DemoBootstrapResponse(
        seeded_entries=result.seeded_entries,
        workloads=result.workloads,
        events_published=result.events_published,
        graph_nodes=result.graph_nodes,
        graph_edges=result.graph_edges,
        interceptor_mode=app_state.interceptor.mode.value,
        environment=app_state.config.environment,
    )


@app.post("/v1/demo/mode", response_model=DemoModeResponse)
async def set_demo_mode(req: DemoModeRequest) -> DemoModeResponse:
    """Update interceptor mode for local/demo walkthroughs."""
    app_state = get_state()
    if _is_production_like(app_state.config.environment):
        raise HTTPException(
            status_code=403,
            detail="demo mode switching is disabled in production/staging",
        )
    if not app_state.accepts_interception:
        raise HTTPException(status_code=503, detail="interceptor disabled for this runtime role")

    app_state.interceptor.mode = DeploymentMode(req.mode)
    return DemoModeResponse(
        interceptor_mode=app_state.interceptor.mode.value,
        environment=app_state.config.environment,
    )


@app.post("/v1/intercept", response_model=InterceptResponse)
async def intercept(request: Request, req: InterceptRequest) -> InterceptResponse | JSONResponse:
    """
    Decision-time interception endpoint with framework-level timeout.

    Defense-in-depth: the interceptor enforces its own 20ms enrichment budget
    internally. This outer wrapper enforces the 50ms total budget at the
    framework level, catching event-loop contention that the internal timer
    cannot detect. On timeout, the request proceeds unmodified (fail-open).
    """
    app_state = get_state()
    if not app_state.accepts_interception:
        raise HTTPException(status_code=503, detail="interceptor disabled for this runtime role")

    aci_headers = _extract_aci_headers(request)
    header_lookup = {key.lower(): value for key, value in aci_headers.items()}
    service_name = req.service_name or header_lookup.get("x-aci-service-id", "")
    route_value = req.route or header_lookup.get("x-aci-route", "")
    route_key = app_state.resolve_route_key(route_value, service_name)

    interception_req = InterceptionRequest(
        request_id=req.request_id,
        model=req.model,
        provider=req.provider,
        service_name=service_name,
        workload_id=req.workload_id or header_lookup.get("x-aci-workload-id", ""),
        api_key_id=req.api_key_id,
        input_tokens=req.input_tokens,
        estimated_cost_usd=req.estimated_cost_usd,
        headers=aci_headers,
        metadata={
            "environment": req.environment or header_lookup.get("x-aci-env", ""),
            "route": route_key,
            "tenant_id": header_lookup.get("x-aci-tenant-id", ""),
            "max_tokens": req.max_tokens,
            "response_format_type": req.response_format_type,
            "tools_present": req.tools_present,
            "strict_json_schema": req.strict_json_schema,
            "price_snapshot_id": app_state.pricing.snapshot_id,
        },
    )

    try:
        interceptor = cast("Any", app_state.interceptor)
        result = await asyncio.wait_for(
            interceptor.intercept(interception_req),
            timeout=app_state.config.interceptor.total_budget_ms / 1000.0,
        )
    except TimeoutError:
        return InterceptResponse(
            request_id=req.request_id,
            outcome=InterceptionOutcome.TIMEOUT.value,
            elapsed_ms=float(app_state.config.interceptor.total_budget_ms),
            enrichment_headers={
                "X-ACI-Fail-Open": "true",
                "X-ACI-Fail-Open-Reason": "LOOKUP_TIMEOUT",
            },
            redirect_model=None,
            attribution_team=None,
            attribution_confidence=None,
        )

    if result.outcome == InterceptionOutcome.HARD_STOPPED:
        gate_error = _gate_error_from_result(result)
        if gate_error is not None:
            return gate_error

    return InterceptResponse(
        request_id=result.request_id,
        outcome=result.outcome.value,
        elapsed_ms=result.elapsed_ms,
        enrichment_headers=result.enrichment_headers,
        redirect_model=result.redirect_model,
        attribution_team=(result.attribution.team_name if result.attribution else None),
        attribution_confidence=(result.attribution.confidence if result.attribution else None),
    )


@app.post("/v1/policy/evaluate", response_model=PolicyEvaluateResponse)
async def policy_evaluate(req: PolicyEvaluateRequest) -> PolicyEvaluateResponse:
    """
    Administrative/test policy evaluation endpoint.

    Hot-path evaluations remain in-process inside the interceptor.
    """
    try:
        context = {
            "model": req.model_requested,
            "confidence": float(req.attribution.get("confidence", 1.0)),
            "current_spend_usd": float(req.signals.est_cost_usd),
            "daily_cost_usd": float(req.signals.est_cost_usd),
            "has_cicd_linkage": bool(req.attribution.get("has_cicd_linkage", True)),
        }
        results = get_state().policy_engine.evaluate_all(context)
    except Exception:
        return PolicyEvaluateResponse(
            decision="PASS_THROUGH",
            advisories=[],
            actions=[],
            fail_open=True,
        )

    advisories = [
        PolicyAdvisoryResponse(code=result.policy_id, detail=result.details)
        for result in results
        if result.violated
    ]
    actions: list[PolicyActionResponse] = []

    active_lite_mode = req.mode.strip().upper() == "ACTIVE_LITE"
    if active_lite_mode and req.service_policy.active_lite_enabled:
        for advisory in advisories:
            actions.append(
                PolicyActionResponse(
                    type=advisory.code.upper(),
                    confidence=float(req.attribution.get("confidence", 0.0)),
                    delta=advisory.detail[:256],
                )
            )

    if actions:
        decision = "APPLY"
    elif advisories:
        decision = "ADVISORY"
    else:
        decision = "PASS_THROUGH"

    return PolicyEvaluateResponse(
        decision=decision,
        advisories=advisories,
        actions=actions,
        fail_open=False,
    )


@app.post("/v1/trac", response_model=TRACResponse)
async def compute_trac(req: TRACRequest) -> TRACResponse:
    """Compute TRAC for a workload."""
    result = get_state().trac.compute(
        workload_id=req.workload_id,
        billed_cost_usd=req.billed_cost_usd,
        emissions_kg_co2e=req.emissions_kg_co2e,
        attribution_confidence=req.attribution_confidence,
        signal_age_days=req.signal_age_days,
    )
    return TRACResponse(
        workload_id=result.workload_id,
        trac_usd=result.trac_usd,
        billed_cost_usd=result.billed_cost_usd,
        carbon_liability_usd=result.carbon_liability_usd,
        confidence_risk_premium_usd=result.confidence_risk_premium_usd,
        carbon_pct_of_trac=result.carbon_pct_of_trac,
        risk_pct_of_trac=result.risk_pct_of_trac,
    )


@app.get("/v1/attribution/{workload_id}", response_model=IndexLookupResponse)
async def get_attribution(workload_id: str) -> IndexLookupResponse:
    """Look up current attribution for a workload."""
    entry = get_state().index_store.lookup(workload_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Workload not found in attribution index")

    return IndexLookupResponse(
        workload_id=entry.workload_id,
        team_id=entry.team_id,
        team_name=entry.team_name,
        cost_center_id=entry.cost_center_id,
        confidence=entry.confidence,
        confidence_tier=entry.confidence_tier,
        method_used=entry.method_used,
        version=entry.version,
    )


@app.get("/v1/index/lookup", response_model=IndexLookupByKeyResponse)
async def index_lookup(key: str) -> IndexLookupByKeyResponse:
    """Low-latency index lookup contract for interceptor callers and diagnostics."""
    entry = get_state().index_store.lookup(key)
    if entry is None:
        raise HTTPException(status_code=404, detail="lookup key not found in attribution index")

    constraints: list[IndexConstraintResponse] = []
    if entry.token_budget_input is not None or entry.token_budget_output is not None:
        constraints.append(IndexConstraintResponse(type="TOKEN_CAP", scope="SERVICE"))
    if entry.model_allowlist:
        constraints.append(IndexConstraintResponse(type="MODEL_ALLOWLIST", scope="SERVICE"))
    if entry.budget_limit_usd is not None:
        constraints.append(IndexConstraintResponse(type="BUDGET_SOFT", scope="TEAM"))
        if entry.budget_remaining_usd is not None and entry.budget_limit_usd > 0:
            utilization = 1.0 - (entry.budget_remaining_usd / entry.budget_limit_usd)
            if utilization >= 1.0:
                constraints.append(IndexConstraintResponse(type="BUDGET_HARD", scope="TEAM"))

    owner_id = entry.team_id or entry.person_id or None
    return IndexLookupByKeyResponse(
        key=key,
        index_version=entry.version,
        attribution=IndexLookupAttributionResponse(
            owner_id=owner_id,
            team_id=entry.team_id or None,
            confidence=entry.confidence,
            reason=_map_attribution_reason(entry.method_used),
            explanation_id=entry.method_used or "unknown",
        ),
        constraints=constraints,
        cache_ttl_ms=60_000,
    )


@app.get("/v1/index/stats")
async def index_stats() -> dict[str, Any]:
    """Attribution index statistics."""
    return get_state().index_store.stats


@app.get("/v1/dashboard/overview", response_model=DashboardOverviewResponse)
async def dashboard_overview() -> DashboardOverviewResponse:
    """Frontend-ready overview metrics for the platform dashboard."""
    app_state = get_state()
    interceptor_stats = app_state.interceptor.stats
    index_stats_payload = app_state.index_store.stats
    event_stats = app_state.event_bus.stats

    return DashboardOverviewResponse(
        tenant_id=app_state.config.tenant_id,
        environment=app_state.config.environment,
        runtime_role=app_state.runtime_role,
        generated_at=datetime.now(UTC).isoformat(),
        index_size=index_stats_payload.get("size", 0),
        index_hit_rate=index_stats_payload.get("hit_rate", 0.0),
        interceptor_requests=interceptor_stats.get("total_requests", 0),
        interceptor_enriched=interceptor_stats.get("total_enriched", 0),
        interceptor_fail_open=interceptor_stats.get("total_fail_open", 0),
        interceptor_cache_misses=interceptor_stats.get("total_cache_misses", 0),
        interceptor_mode=interceptor_stats.get("mode", "advisory"),
        circuit_breaker_state=interceptor_stats.get("circuit_breaker_state", "closed"),
        events_published=event_stats.get("published", 0),
    )


@app.get("/v1/pricing/catalog", response_model=list[PricingRuleResponse])
async def pricing_catalog(
    provider: str | None = None,
    model: str | None = None,
) -> list[PricingRuleResponse]:
    """List versioned pricing rules used by synthetic and simulation flows."""
    rules = get_state().pricing.list_rules(provider=provider, model=model)
    return [
        PricingRuleResponse(
            provider=rule.provider,
            model=rule.model,
            effective_from=rule.effective_from.isoformat(),
            input_usd_per_1k_tokens=rule.input_usd_per_1k_tokens,
            output_usd_per_1k_tokens=rule.output_usd_per_1k_tokens,
            cached_input_usd_per_1k_tokens=rule.cached_input_usd_per_1k_tokens,
            request_base_usd=rule.request_base_usd,
        )
        for rule in rules
    ]


@app.post("/v1/pricing/estimate", response_model=PricingEstimateResponse)
async def pricing_estimate(req: PricingEstimateRequest) -> PricingEstimateResponse:
    """Estimate request costs from usage + versioned pricing (FR-110/111)."""
    usage = PricingUsage(
        provider=req.provider,
        model=req.model,
        input_tokens=req.input_tokens,
        output_tokens=req.output_tokens,
        cache_read_input_tokens=req.cache_read_input_tokens,
        request_count=req.request_count,
        effective_at=req.effective_at,
    )
    try:
        estimate = get_state().pricing.estimate(usage)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return PricingEstimateResponse(
        provider=estimate.provider,
        model=estimate.model,
        request_count=estimate.request_count,
        rule_effective_from=estimate.rule_effective_from.isoformat(),
        input_cost_usd=estimate.input_cost_usd,
        cached_input_cost_usd=estimate.cached_input_cost_usd,
        output_cost_usd=estimate.output_cost_usd,
        request_base_cost_usd=estimate.request_base_cost_usd,
        total_cost_usd=estimate.total_cost_usd,
    )


@app.post("/v1/finops/synthetic", response_model=SyntheticCostIngestResponse)
async def finops_synthetic_cost(req: SyntheticCostIngestRequest) -> SyntheticCostIngestResponse:
    """
    Record real-time synthetic cost for a request (FR-111).

    If synthetic_cost_usd is omitted, compute deterministically from pricing rules.
    """
    app_state = get_state()
    synthetic_cost = req.synthetic_cost_usd
    if synthetic_cost is None:
        usage = PricingUsage(
            provider=req.provider,
            model=req.model,
            input_tokens=req.input_tokens,
            output_tokens=req.output_tokens,
            cache_read_input_tokens=req.cache_read_input_tokens,
            request_count=1,
            effective_at=req.recorded_at,
        )
        try:
            synthetic_cost = app_state.pricing.estimate(usage).total_cost_usd
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    record = app_state.reconciliation.upsert_synthetic(
        request_id=req.request_id,
        service_name=req.service_name,
        provider=req.provider,
        model=req.model,
        synthetic_cost_usd=synthetic_cost,
        recorded_at=req.recorded_at,
    )
    return SyntheticCostIngestResponse(
        request_id=record.request_id,
        synthetic_cost_usd=record.synthetic_cost_usd,
        provider=record.provider,
        model=record.model,
        recorded_at=record.synthetic_recorded_at.isoformat(),
        reconciled=record.is_reconciled,
    )


@app.post("/v1/finops/reconcile", response_model=CostReconcileResponse)
async def finops_reconcile(req: CostReconcileRequest) -> CostReconcileResponse:
    """Attach billing truth-up cost and expose drift for the request (FR-111/607)."""
    try:
        record = get_state().reconciliation.reconcile(
            request_id=req.request_id,
            reconciled_cost_usd=req.reconciled_cost_usd,
            source=req.source,
            recorded_at=req.recorded_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    drift_usd = record.drift_usd or 0.0
    drift_pct = record.drift_pct or 0.0
    return CostReconcileResponse(
        request_id=record.request_id,
        synthetic_cost_usd=record.synthetic_cost_usd,
        reconciled_cost_usd=record.reconciled_cost_usd or 0.0,
        drift_usd=drift_usd,
        drift_pct=drift_pct,
        source=record.reconciliation_source,
    )


@app.get("/v1/finops/drift", response_model=list[DriftSummaryResponse])
async def finops_drift(
    group_by: str = "provider",
    window: Literal["daily", "weekly"] = "daily",
    daily_threshold_pct: float = 3.0,
    weekly_threshold_pct: float = 5.0,
) -> list[DriftSummaryResponse]:
    """Aggregate reconciliation drift and flag investigation thresholds (FR-607)."""
    try:
        summaries = get_state().reconciliation.summarize_drift(group_by=group_by)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    threshold_pct = daily_threshold_pct if window == "daily" else weekly_threshold_pct
    return [
        DriftSummaryResponse(
            group=summary.group,
            total_records=summary.total_records,
            reconciled_records=summary.reconciled_records,
            unresolved_records=summary.unresolved_records,
            synthetic_cost_usd=summary.synthetic_cost_usd,
            reconciled_cost_usd=summary.reconciled_cost_usd,
            absolute_drift_usd=summary.absolute_drift_usd,
            drift_pct=summary.drift_pct,
            window=window,
            threshold_pct=threshold_pct,
            investigation_required=abs(summary.drift_pct) > threshold_pct,
            dashboard_annotation=(
                "unreconciled" if abs(summary.drift_pct) > threshold_pct else "reconciled"
            ),
        )
        for summary in summaries
    ]


@app.post("/v1/forecast/spend", response_model=SpendForecastResponse)
async def forecast_spend(req: SpendForecastRequest) -> SpendForecastResponse:
    """Forecast monthly spend from recent time-series history (FR-408)."""
    try:
        forecast = get_state().forecast.forecast(
            monthly_spend_usd=req.monthly_spend_usd,
            horizon_months=req.horizon_months,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return SpendForecastResponse(
        trend_pct=forecast.trend_pct,
        residual_stddev_usd=forecast.residual_stddev_usd,
        points=[
            SpendForecastPointResponse(
                month_offset=point.month_offset,
                predicted_spend_usd=point.predicted_spend_usd,
                lower_bound_usd=point.lower_bound_usd,
                upper_bound_usd=point.upper_bound_usd,
            )
            for point in forecast.points
        ],
    )


@app.get("/v1/interventions", response_model=InterventionListResponse)
async def list_interventions(status: str | None = None) -> InterventionListResponse:
    """List intervention recommendations and lifecycle summary (FR-502)."""
    normalized_status = status.strip().lower() if status else None
    registry = get_state().intervention_registry

    try:
        records = registry.list_records(status=normalized_status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    summary = registry.summary()
    return InterventionListResponse(
        summary=InterventionSummaryResponse(
            total_count=summary.total_count,
            recommended_count=summary.recommended_count,
            review_count=summary.review_count,
            approved_count=summary.approved_count,
            implemented_count=summary.implemented_count,
            dismissed_count=summary.dismissed_count,
            total_potential_usd=summary.total_potential_usd,
            captured_savings_usd=summary.captured_savings_usd,
            open_potential_usd=summary.open_potential_usd,
        ),
        interventions=[_to_intervention_response(record) for record in records],
    )


@app.get("/v1/interventions/summary", response_model=InterventionSummaryResponse)
async def intervention_summary() -> InterventionSummaryResponse:
    """Return aggregate intervention lifecycle metrics (FR-404/502)."""
    summary = get_state().intervention_registry.summary()
    return InterventionSummaryResponse(
        total_count=summary.total_count,
        recommended_count=summary.recommended_count,
        review_count=summary.review_count,
        approved_count=summary.approved_count,
        implemented_count=summary.implemented_count,
        dismissed_count=summary.dismissed_count,
        total_potential_usd=summary.total_potential_usd,
        captured_savings_usd=summary.captured_savings_usd,
        open_potential_usd=summary.open_potential_usd,
    )


@app.post("/v1/interventions/{intervention_id}/status", response_model=InterventionResponse)
async def set_intervention_status(
    intervention_id: str, req: InterventionTransitionRequest
) -> InterventionResponse:
    """Transition intervention lifecycle state with audit metadata (FR-502)."""
    try:
        updated = get_state().intervention_registry.transition(
            intervention_id=intervention_id,
            next_status=req.status,
            actor=req.actor,
            note=req.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _to_intervention_response(updated)


@app.post("/v1/interventions/cost-simulate", response_model=CostSimulationResponse)
async def simulate_intervention_cost(req: CostSimulationRequest) -> CostSimulationResponse:
    """Pre-deploy cost simulation for model routing decisions (FR-503/606)."""
    try:
        result = get_state().cost_simulation.simulate(
            service_id=req.service_id,
            provider=req.provider,
            current_model=req.current_model,
            avg_input_tokens=req.avg_input_tokens,
            avg_output_tokens=req.avg_output_tokens,
            requests_per_day=req.requests_per_day,
            candidate_models=req.candidate_models,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return CostSimulationResponse(
        service_id=result.service_id,
        provider=result.provider,
        current_model=result.current_model,
        projected_monthly_cost_usd=result.projected_monthly_cost_usd,
        candidates=[
            CostSimulationCandidateResponse(
                model=candidate.model,
                projected_monthly_cost_usd=candidate.projected_monthly_cost_usd,
                monthly_savings_usd=candidate.monthly_savings_usd,
                savings_pct=candidate.savings_pct,
            )
            for candidate in result.candidates
        ],
    )


@app.post("/v1/integrations/notify", response_model=list[NotificationDeliveryResponse])
async def integrations_notify(req: NotificationRequest) -> list[NotificationDeliveryResponse]:
    """Dispatch policy/intervention notifications via Slack/email/webhook (FR-800/801/803)."""
    message = NotificationMessage(
        event_type=req.event_type,
        title=req.title,
        detail=req.detail,
        severity=req.severity,
        metadata=req.metadata,
    )
    deliveries = get_state().notification_hub.send(
        message=message,
        channels=req.channels,
        slack_webhook_url=req.slack_webhook_url,
        webhook_url=req.webhook_url,
        email_to=req.email_to,
    )
    return [
        NotificationDeliveryResponse(
            delivery_id=delivery.delivery_id,
            channel=delivery.channel,
            target=delivery.target,
            status=delivery.status,
            message=delivery.message,
            sent_at=delivery.sent_at.isoformat(),
        )
        for delivery in deliveries
    ]


@app.get("/v1/integrations/deliveries", response_model=list[NotificationDeliveryResponse])
async def integrations_deliveries(limit: int = 100) -> list[NotificationDeliveryResponse]:
    """Inspect recent notification deliveries for auditability."""
    deliveries = get_state().notification_hub.list_deliveries(limit=limit)
    return [
        NotificationDeliveryResponse(
            delivery_id=delivery.delivery_id,
            channel=delivery.channel,
            target=delivery.target,
            status=delivery.status,
            message=delivery.message,
            sent_at=delivery.sent_at.isoformat(),
        )
        for delivery in deliveries
    ]
