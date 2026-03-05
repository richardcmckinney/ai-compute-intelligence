"""
FastAPI application: API surface for the ACI platform.

Routes serve attribution data, policy management, benchmarks, and health checks.
The API runs in the vendor control plane (SaaS) or within customer VPC depending
on deployment topology.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from jwt import InvalidTokenError
from pydantic import BaseModel, Field

from aci.api.auth import (
    can_bypass_auth,
    decode_and_validate_token,
    is_auth_required,
    is_public_path,
)
from aci.confidence.calibration import CalibrationEngine
from aci.config import PlatformConfig
from aci.core.event_bus import (
    AsyncIdempotencyStore,
    InMemoryEventBus,
    InMemoryIdempotencyStore,
    KafkaEventBus,
    RedisIdempotencyStore,
)
from aci.core.event_schema import EventSchemaValidationError, validate_event_attributes
from aci.core.processor import AttributionProcessor
from aci.equivalence.verifier import EquivalenceVerifier
from aci.graph.store import GraphStore
from aci.hre.engine import HeuristicReconciliationEngine
from aci.index.materializer import AttributionIndexStore, IndexMaterializer
from aci.interceptor.circuit_breaker import RedisCircuitStateStore
from aci.interceptor.gateway import (
    DeploymentMode,
    FailOpenInterceptor,
    InterceptionOutcome,
    InterceptionRequest,
)
from aci.models.events import DomainEvent, EventType
from aci.policy.engine import PolicyEngine
from aci.trac.calculator import TRACCalculator

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Application state (initialized at startup)
# ---------------------------------------------------------------------------


class AppState:
    """Shared application state across request handlers."""

    def __init__(self) -> None:
        self.config = PlatformConfig()
        self.runtime_role = self.config.runtime_role.lower()
        self.accepts_ingestion = self.runtime_role in {"all", "processor"}
        self.accepts_interception = self.runtime_role in {"all", "gateway"}
        self.event_bus = self._build_event_bus()
        self.graph = GraphStore()

        self.index_store = AttributionIndexStore(
            max_entries=self.config.index_max_entries,
            redis_url=self.config.redis_url if self.config.index_backend == "redis" else None,
            redis_prefix=self.config.index_redis_prefix,
        )
        self.materializer = IndexMaterializer(self.index_store, self.config)
        self.hre = HeuristicReconciliationEngine()
        self.calibration = CalibrationEngine(self.config.confidence)
        self.trac = TRACCalculator(self.config.trac, self.config.confidence)
        self.policy_engine = PolicyEngine()
        self.equivalence = EquivalenceVerifier(self.config.equivalence)

        # Closed-loop processor: ingestion -> reconciliation -> materialization.
        self.processor = AttributionProcessor(
            event_bus=self.event_bus,
            graph_store=self.graph,
            hre=self.hre,
            calibration=self.calibration,
            materializer=self.materializer,
            policy_engine=self.policy_engine,
        )

        circuit_state_store = None
        if self.config.interceptor.circuit_state_backend == "redis":
            circuit_state_store = RedisCircuitStateStore(
                redis_url=self.config.redis_url,
                key=self.config.interceptor.circuit_state_redis_key,
            )

        self.interceptor = FailOpenInterceptor(
            self.index_store,
            self.config.interceptor,
            mode=DeploymentMode.ADVISORY,
            event_bus=self.event_bus,
            circuit_state_store=circuit_state_store,
        )

    def _build_event_bus(self) -> InMemoryEventBus | KafkaEventBus:
        backend = self.config.event_bus_backend.lower()
        if backend == "kafka":
            idempotency_store: AsyncIdempotencyStore
            if self.config.redis_url:
                idempotency_store = RedisIdempotencyStore(
                    redis_url=self.config.redis_url,
                    ttl_seconds=self.config.event_bus_dedup_ttl_s,
                )
            else:
                idempotency_store = InMemoryIdempotencyStore(max_keys=2_000_000)
            return KafkaEventBus(
                bootstrap_servers=self.config.kafka_bootstrap,
                topic=self.config.event_bus_topic,
                dlq_topic=self.config.event_bus_dlq_topic,
                consumer_group=self.config.event_bus_consumer_group,
                idempotency_store=idempotency_store,
            )

        return InMemoryEventBus()

    async def start(self) -> None:
        await self.event_bus.start()

    async def stop(self) -> None:
        await self.event_bus.stop()

    def readiness_checks(self) -> dict[str, bool]:
        """Evaluate lightweight readiness checks for probe endpoints."""
        checks = {
            "event_bus_started": bool(getattr(self.event_bus, "is_started", True)),
        }

        if self.config.index_backend.lower() == "redis":
            checks["index_durable_backend_healthy"] = self.index_store.durable_backend_healthy()
        else:
            checks["index_durable_backend_healthy"] = True

        return checks


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialize and tear down platform components."""
    del app
    logger.info("platform.starting", tenant=state.config.tenant_id)
    await state.start()
    yield
    await state.stop()
    logger.info("platform.shutting_down")


app = FastAPI(
    title="ACI Platform",
    description="AI Compute Intelligence: application-level cost and carbon attribution",
    version="0.2.0",
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=500)


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

    auth_config = state.config.auth
    if not auth_config.enabled:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        if can_bypass_auth(state.config):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "missing bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header.removeprefix("Bearer ").strip()
    try:
        claims = decode_and_validate_token(token, auth_config, state.config.tenant_id)
    except InvalidTokenError as exc:
        logger.warning("auth.token_invalid", error=str(exc), path=path)
        return JSONResponse(
            status_code=401,
            content={"detail": "invalid bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.auth_claims = claims
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
    api_key_id: str = ""
    input_tokens: int = 0
    estimated_cost_usd: float = 0.0


class InterceptResponse(BaseModel):
    request_id: str
    outcome: str
    elapsed_ms: float
    enrichment_headers: dict[str, str]
    redirect_model: str | None = None
    attribution_team: str | None = None
    attribution_confidence: float | None = None


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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


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
    checks = state.readiness_checks()
    status = "healthy" if all(checks.values()) else "degraded"
    return HealthResponse(
        status=status,
        timestamp=datetime.now(UTC).isoformat(),
        index_size=state.index_store.size,
        runtime_role=state.runtime_role,
        interceptor_mode=state.interceptor.mode.value,
        circuit_breaker=state.interceptor.circuit_breaker.state,
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
    checks = state.readiness_checks()
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
    return {
        "index": state.index_store.stats,
        "interceptor": state.interceptor.stats,
        "processor": state.processor.stats,
        "event_bus": state.event_bus.stats,
    }


@app.post("/v1/events/ingest", response_model=EventIngestResponse)
async def ingest_event(req: EventIngestRequest) -> EventIngestResponse:
    """Ingest a single domain event into the append-only bus."""
    if not state.accepts_ingestion:
        raise HTTPException(
            status_code=503, detail="event ingestion disabled for this runtime role"
        )

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
        tenant_id=state.config.tenant_id,
    )
    accepted = await state.event_bus.publish(event)
    return EventIngestResponse(accepted=accepted, event_id=event.event_id)


@app.post("/v1/events/ingest/batch", response_model=EventBatchIngestResponse)
async def ingest_events_batch(req: EventBatchIngestRequest) -> EventBatchIngestResponse:
    """Ingest a batch of domain events with idempotency-aware accounting."""
    if not state.accepts_ingestion:
        raise HTTPException(
            status_code=503, detail="event ingestion disabled for this runtime role"
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
                tenant_id=state.config.tenant_id,
            )
        )
    result = await state.event_bus.publish_batch(events)
    return EventBatchIngestResponse(
        total=len(events),
        accepted=result["published"],
        deduplicated=result["deduplicated"],
    )


@app.post("/v1/intercept", response_model=InterceptResponse)
async def intercept(req: InterceptRequest) -> InterceptResponse:
    """
    Decision-time interception endpoint with framework-level timeout.

    Defense-in-depth: the interceptor enforces its own 20ms enrichment budget
    internally. This outer wrapper enforces the 50ms total budget at the
    framework level, catching event-loop contention that the internal timer
    cannot detect. On timeout, the request proceeds unmodified (fail-open).
    """
    if not state.accepts_interception:
        raise HTTPException(status_code=503, detail="interceptor disabled for this runtime role")

    interception_req = InterceptionRequest(
        request_id=req.request_id,
        model=req.model,
        provider=req.provider,
        service_name=req.service_name,
        api_key_id=req.api_key_id,
        input_tokens=req.input_tokens,
        estimated_cost_usd=req.estimated_cost_usd,
    )

    try:
        result = await asyncio.wait_for(
            state.interceptor.intercept(interception_req),
            timeout=state.config.interceptor.total_budget_ms / 1000.0,
        )
    except TimeoutError:
        return InterceptResponse(
            request_id=req.request_id,
            outcome=InterceptionOutcome.TIMEOUT.value,
            elapsed_ms=float(state.config.interceptor.total_budget_ms),
            enrichment_headers={},
            redirect_model=None,
            attribution_team=None,
            attribution_confidence=None,
        )

    return InterceptResponse(
        request_id=result.request_id,
        outcome=result.outcome.value,
        elapsed_ms=result.elapsed_ms,
        enrichment_headers=result.enrichment_headers,
        redirect_model=result.redirect_model,
        attribution_team=(result.attribution.team_name if result.attribution else None),
        attribution_confidence=(result.attribution.confidence if result.attribution else None),
    )


@app.post("/v1/trac", response_model=TRACResponse)
async def compute_trac(req: TRACRequest) -> TRACResponse:
    """Compute TRAC for a workload."""
    result = state.trac.compute(
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
    entry = state.index_store.lookup(workload_id)
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


@app.get("/v1/index/stats")
async def index_stats() -> dict[str, Any]:
    """Attribution index statistics."""
    return state.index_store.stats


@app.get("/v1/dashboard/overview", response_model=DashboardOverviewResponse)
async def dashboard_overview() -> DashboardOverviewResponse:
    """Frontend-ready overview metrics for the platform dashboard."""
    interceptor_stats = state.interceptor.stats
    index_stats_payload = state.index_store.stats
    event_stats = state.event_bus.stats

    return DashboardOverviewResponse(
        tenant_id=state.config.tenant_id,
        environment=state.config.environment,
        runtime_role=state.runtime_role,
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
