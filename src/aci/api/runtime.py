"""Application runtime assembly and role-aware state management."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

from aci.confidence.calibration import CalibrationEngine
from aci.config import PlatformConfig
from aci.core.event_bus import (
    AsyncIdempotencyStore,
    InMemoryEventBus,
    InMemoryIdempotencyStore,
    KafkaEventBus,
    RedisIdempotencyStore,
)
from aci.core.processor import AttributionProcessor
from aci.equivalence.verifier import EquivalenceVerifier
from aci.finops.reconciliation import ReconciliationLedger
from aci.forecast.engine import SpendForecastEngine
from aci.graph.store import GraphStoreProtocol, build_graph_store
from aci.hre.engine import HeuristicReconciliationEngine
from aci.index.materializer import AttributionIndexStore, IndexMaterializer
from aci.integrations.notifications import NotificationHub
from aci.interceptor.circuit_breaker import RedisCircuitStateStore
from aci.interceptor.gateway import DeploymentMode, FailOpenInterceptor
from aci.interventions.registry import InterventionRegistry
from aci.interventions.simulator import CostSimulationEngine
from aci.policy.engine import PolicyEngine
from aci.pricing.catalog import PricingCatalog
from aci.trac.calculator import TRACCalculator

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import FastAPI, Request


class SlidingWindowRateLimiter:
    """Simple in-memory sliding window limiter for ingestion endpoints."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._ops_since_cleanup = 0
        self._cleanup_interval_ops = 256

    async def allow(self, key: str) -> bool:
        now = datetime.now(UTC).timestamp()
        cutoff = now - self._window_seconds
        async with self._lock:
            bucket = self._events.get(key)
            if bucket is None:
                bucket = deque()
                self._events[key] = bucket

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            self._ops_since_cleanup += 1
            if self._ops_since_cleanup >= self._cleanup_interval_ops:
                self._ops_since_cleanup = 0
                stale_keys = [
                    candidate_key
                    for candidate_key, candidate_bucket in self._events.items()
                    if candidate_key != key
                    and (not candidate_bucket or candidate_bucket[-1] <= cutoff)
                ]
                for stale_key in stale_keys:
                    self._events.pop(stale_key, None)

            if len(bucket) >= self._limit:
                return False

            bucket.append(now)
            return True


class DisabledEventBus:
    """No-op event bus used for gateway-only runtimes."""

    is_started = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def publish(self, *_args: object, **_kwargs: object) -> bool:
        return False

    async def publish_batch(self, _events: Sequence[object]) -> dict[str, int]:
        return {"published": 0, "deduplicated": 0}

    def subscribe(self, _topic: str, _handler: object) -> None:
        return None

    @property
    def stats(self) -> dict[str, object]:
        return {
            "backend": "disabled",
            "started": False,
            "total_events": 0,
            "published": 0,
            "deduplicated": 0,
            "idempotency_cache_size": 0,
            "idempotency_evictions": 0,
            "dispatch_errors": 0,
            "topics": [],
            "published_by_topic": {},
        }


class DisabledProcessor:
    """No-op processor used for gateway-only runtimes."""

    @property
    def stats(self) -> dict[str, int]:
        return {
            "processed_events": 0,
            "graph_nodes": 0,
            "graph_edges": 0,
            "index_size": 0,
        }


class DisabledInterceptor:
    """No-op interceptor used for processor-only runtimes."""

    def __init__(self, mode: DeploymentMode) -> None:
        self.mode = mode
        self.circuit_breaker = SimpleNamespace(state="disabled")

    async def shutdown(self) -> None:
        return None

    @property
    def stats(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "total_requests": 0,
            "total_enriched": 0,
            "total_fail_open": 0,
            "total_redirected": 0,
            "total_cache_misses": 0,
            "circuit_breaker_state": "disabled",
        }


class AppState:
    """Shared application state assembled according to the selected runtime role."""

    def __init__(self) -> None:
        self.config = PlatformConfig()
        self.runtime_role = self.config.runtime_role.lower()
        self.accepts_ingestion = self.runtime_role in {"all", "processor"}
        self.accepts_interception = self.runtime_role in {"all", "gateway"}
        self.is_demo_environment = self.config.environment.lower() == "demo"

        self.ingest_rate_limiter = SlidingWindowRateLimiter(
            self.config.api_ingest_rate_limit_per_minute
        )
        self.ingest_max_batch_size = self.config.api_ingest_max_batch_size
        self._route_cardinality_limit = 50_000
        self._route_templates: set[str] = set()
        self._route_lock = threading.Lock()

        self.index_store = AttributionIndexStore(
            max_entries=self.config.index_max_entries,
            redis_url=self.config.redis_url if self.config.index_backend == "redis" else None,
            redis_prefix=self.config.index_redis_prefix,
            redis_ttl_seconds=self.config.index_redis_ttl_s,
        )
        self.pricing = PricingCatalog.with_default_rules()
        self.reconciliation = ReconciliationLedger()
        self.forecast = SpendForecastEngine()
        self.notification_hub = NotificationHub()
        self.cost_simulation = CostSimulationEngine(self.pricing)
        self.intervention_registry = InterventionRegistry.with_seed_data()
        self.trac = TRACCalculator(self.config.trac, self.config.confidence)
        self.policy_engine = PolicyEngine()
        self.equivalence = EquivalenceVerifier(self.config.equivalence)

        self.event_bus: InMemoryEventBus | KafkaEventBus | DisabledEventBus
        self.graph: GraphStoreProtocol | None = None
        self.materializer: IndexMaterializer | None = None
        self.hre: HeuristicReconciliationEngine | None = None
        self.calibration: CalibrationEngine | None = None
        self.processor: AttributionProcessor | DisabledProcessor
        self.interceptor: FailOpenInterceptor | DisabledInterceptor

        if self.accepts_ingestion:
            self.event_bus = self._build_event_bus()
            self.graph = build_graph_store(self.config)
            self.materializer = IndexMaterializer(self.index_store, self.config)
            self.hre = HeuristicReconciliationEngine()
            self.calibration = CalibrationEngine(self.config.confidence)
            self.processor = AttributionProcessor(
                event_bus=self.event_bus,
                graph_store=self.graph,
                hre=self.hre,
                calibration=self.calibration,
                materializer=self.materializer,
                policy_engine=self.policy_engine,
            )
        else:
            self.event_bus = DisabledEventBus()
            self.processor = DisabledProcessor()

        if self.accepts_interception:
            circuit_state_store = None
            if self.config.interceptor.circuit_state_backend == "redis":
                circuit_state_store = RedisCircuitStateStore(
                    redis_url=self.config.redis_url,
                    key=self.config.interceptor.circuit_state_redis_key,
                )
            self.interceptor = FailOpenInterceptor(
                self.index_store,
                self.config.interceptor,
                mode=DeploymentMode(self.config.interceptor_mode.lower()),
                event_bus=self.event_bus if self.accepts_ingestion else None,
                circuit_state_store=circuit_state_store,
            )
        else:
            self.interceptor = DisabledInterceptor(
                mode=DeploymentMode(self.config.interceptor_mode.lower())
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
        if self.graph is not None:
            self.graph.start()
        await self.event_bus.start()
        if self.is_demo_environment:
            from aci.demo.seeder import bootstrap_demo_state

            await bootstrap_demo_state(self, reset_existing=False)

    async def stop(self) -> None:
        await self.interceptor.shutdown()
        await self.event_bus.stop()
        if self.graph is not None:
            self.graph.close()

    def readiness_checks(self) -> dict[str, bool]:
        checks = {
            "event_bus_started": True,
            "graph_backend_healthy": True,
        }
        if self.accepts_ingestion:
            checks["event_bus_started"] = bool(getattr(self.event_bus, "is_started", True))
            checks["graph_backend_healthy"] = self.graph.ready() if self.graph is not None else True

        if self.config.index_backend.lower() == "redis":
            checks["index_durable_backend_healthy"] = self.index_store.durable_backend_healthy()
        else:
            checks["index_durable_backend_healthy"] = True

        return checks

    def resolve_route_key(self, route: str, service_name: str) -> str:
        normalized_route = _normalize_route(route)
        if not normalized_route:
            return f"SERVICE:{service_name}" if service_name else ""

        with self._route_lock:
            if len(self._route_templates) >= self._route_cardinality_limit:
                return f"SERVICE:{service_name}" if service_name else normalized_route
            self._route_templates.add(normalized_route)

        return normalized_route


_APP_STATE_LOCK = threading.Lock()


def ensure_app_state(app: FastAPI) -> AppState:
    """Attach and return application state for the provided FastAPI instance."""
    state = getattr(app.state, "platform_state", None)
    if state is not None:
        if not isinstance(state, AppState):
            raise TypeError("app.state.platform_state must be an AppState instance")
        return state

    with _APP_STATE_LOCK:
        state = getattr(app.state, "platform_state", None)
        if state is None:
            state = AppState()
            app.state.platform_state = state
        if not isinstance(state, AppState):
            raise TypeError("app.state.platform_state must be an AppState instance")
        return state


def get_request_state(request: Request) -> AppState:
    """FastAPI dependency for request-scoped access to app-attached state."""
    return ensure_app_state(request.app)


def _normalize_route(route: str) -> str:
    normalized = (route or "").strip().lower()
    if not normalized:
        return ""
    return "/" + "/".join(segment for segment in normalized.split("/") if segment)
