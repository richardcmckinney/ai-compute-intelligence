"""
Locust load test for Phase 0 Glass Jaw sustained validation.

Run for 72 hours at 10K req/s against the interceptor endpoint.
Validates:
- P99 latency overhead <= 15ms
- Zero request failures
- Memory footprint <= 256MB under sustained load

Usage:
    locust -f tests/glass_jaw/locustfile.py --host=http://localhost:8000 \
        --users=500 --spawn-rate=50 --run-time=72h --headless \
        --csv=results/glass_jaw
"""

from __future__ import annotations

import random
import uuid

from locust import HttpUser, between, task


class InterceptorUser(HttpUser):
    """Simulates inference requests hitting the interceptor endpoint."""

    wait_time = between(0.001, 0.01)  # type: ignore[no-untyped-call]

    MODELS = ["gpt-4o", "gpt-4o-mini", "claude-3-haiku", "claude-3.5-sonnet"]
    SERVICES = [f"service-{i}" for i in range(1000)]

    @task(10)
    def intercept_known_service(self) -> None:
        """Request for a service that exists in the index (cache hit path)."""
        self.client.post(
            "/v1/intercept",
            json={
                "request_id": str(uuid.uuid4()),
                "model": random.choice(self.MODELS),
                "provider": "openai",
                "service_name": random.choice(self.SERVICES),
                "estimated_cost_usd": random.uniform(0.001, 0.05),
            },
            name="/v1/intercept [hit]",
        )

    @task(3)
    def intercept_unknown_service(self) -> None:
        """Request for an unknown service (cache miss / fail-open path)."""
        self.client.post(
            "/v1/intercept",
            json={
                "request_id": str(uuid.uuid4()),
                "model": random.choice(self.MODELS),
                "provider": "openai",
                "service_name": f"unknown-{uuid.uuid4().hex[:8]}",
                "estimated_cost_usd": random.uniform(0.001, 0.05),
            },
            name="/v1/intercept [miss]",
        )

    @task(1)
    def health_check(self) -> None:
        """Periodic health check."""
        self.client.get("/health")

    @task(1)
    def compute_trac(self) -> None:
        """TRAC computation endpoint."""
        self.client.post(
            "/v1/trac",
            json={
                "workload_id": random.choice(self.SERVICES),
                "billed_cost_usd": random.uniform(10, 500),
                "emissions_kg_co2e": random.uniform(0.01, 5.0),
                "attribution_confidence": random.uniform(0.3, 0.95),
            },
            name="/v1/trac",
        )
