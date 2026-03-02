"""
FastAPI application: API surface for the ACI platform.

Routes serve attribution data, policy management, benchmarks, and health checks.
The API runs in the vendor control plane (SaaS) or within customer VPC depending
on deployment topology.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from aci.config import PlatformConfig
from aci.confidence.calibration import CalibrationEngine
from aci.equivalence.verifier import EquivalenceVerifier
from aci.hre.engine import HeuristicReconciliationEngine
from aci.index.materializer import AttributionIndexStore, IndexMaterializer
from aci.interceptor.gateway import (
    DeploymentMode,
    FailOpenInterceptor,
    InterceptionOutcome,
    InterceptionRequest,
)
from aci.policy.engine import PolicyEngine
from aci.trac.calculator import TRACCalculator

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Application state (initialized at startup)
# ---------------------------------------------------------------------------

class AppState:
    """Shared application state across request handlers."""

    def __init__(self) -> None:
        self.config = PlatformConfig()
        self.index_store = AttributionIndexStore()
        self.materializer = IndexMaterializer(self.index_store, self.config)
        self.hre = HeuristicReconciliationEngine()
        self.calibration = CalibrationEngine(self.config.confidence)
        self.trac = TRACCalculator(self.config.trac, self.config.confidence)
        self.policy_engine = PolicyEngine()
        self.equivalence = EquivalenceVerifier(self.config.equivalence)
        self.interceptor = FailOpenInterceptor(
            self.index_store,
            self.config.interceptor,
            mode=DeploymentMode.ADVISORY,
        )


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize and tear down platform components."""
    logger.info("platform.starting", tenant=state.config.tenant_id)
    yield
    logger.info("platform.shutting_down")


app = FastAPI(
    title="ACI Platform",
    description="AI Compute Intelligence: application-level cost and carbon attribution",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    index_size: int
    interceptor_mode: str
    circuit_breaker: str


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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health():
    """Platform health check."""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).isoformat(),
        index_size=state.index_store.size,
        interceptor_mode=state.interceptor.mode.value,
        circuit_breaker=state.interceptor.circuit_breaker.state,
    )


@app.get("/metrics")
async def metrics():
    """Platform metrics for monitoring."""
    return {
        "index": state.index_store.stats,
        "interceptor": state.interceptor.stats,
    }


@app.post("/v1/intercept", response_model=InterceptResponse)
async def intercept(req: InterceptRequest):
    """
    Decision-time interception endpoint with framework-level timeout.

    Defense-in-depth: the interceptor enforces its own 20ms enrichment budget
    internally. This outer wrapper enforces the 50ms total budget at the
    framework level, catching event-loop contention that the internal timer
    cannot detect. On timeout, the request proceeds unmodified (fail-open).
    """
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
    except asyncio.TimeoutError:
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
        attribution_team=(
            result.attribution.team_name if result.attribution else None
        ),
        attribution_confidence=(
            result.attribution.confidence if result.attribution else None
        ),
    )


@app.post("/v1/trac", response_model=TRACResponse)
async def compute_trac(req: TRACRequest):
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
async def get_attribution(workload_id: str):
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
async def index_stats():
    """Attribution index statistics."""
    return state.index_store.stats
