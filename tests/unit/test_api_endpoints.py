from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from jwt import encode
from pydantic import SecretStr

from aci.api.app import SlidingWindowRateLimiter, app, get_state
from aci.interceptor.gateway import DeploymentMode
from aci.models.attribution import AttributionIndexEntry
from aci.models.carbon import EnforcementAction, PolicyDefinition, PolicyType

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def isolated_app_state() -> Generator[None, None, None]:
    app_state = get_state()
    original_environment = app_state.config.environment
    original_auth_enabled = app_state.config.auth.enabled
    original_allow_dev_bypass = app_state.config.auth.allow_dev_bypass
    original_algorithm = app_state.config.auth.jwt_algorithm
    original_secret = app_state.config.auth.jwt_hs256_secret
    original_issuer = app_state.config.auth.jwt_issuer
    original_audience = app_state.config.auth.jwt_audience
    original_required_scope = app_state.config.auth.required_scope
    original_tenant_claim = app_state.config.auth.tenant_claim
    original_runtime_role = app_state.runtime_role
    original_accepts_ingestion = app_state.accepts_ingestion
    original_accepts_interception = app_state.accepts_interception
    original_batch_limit = app_state.ingest_max_batch_size
    original_limiter = app_state.ingest_rate_limiter
    original_mode = app_state.interceptor.mode

    app_state.index_store.clear()
    if app_state.graph is not None:
        app_state.graph.clear()
    app_state.policy_engine.policies.clear()

    yield

    app_state.index_store.clear()
    if app_state.graph is not None:
        app_state.graph.clear()
    app_state.policy_engine.policies.clear()
    app_state.config.environment = original_environment
    app_state.config.auth.enabled = original_auth_enabled
    app_state.config.auth.allow_dev_bypass = original_allow_dev_bypass
    app_state.config.auth.jwt_algorithm = original_algorithm
    app_state.config.auth.jwt_hs256_secret = original_secret
    app_state.config.auth.jwt_issuer = original_issuer
    app_state.config.auth.jwt_audience = original_audience
    app_state.config.auth.required_scope = original_required_scope
    app_state.config.auth.tenant_claim = original_tenant_claim
    app_state.runtime_role = original_runtime_role
    app_state.accepts_ingestion = original_accepts_ingestion
    app_state.accepts_interception = original_accepts_interception
    app_state.ingest_max_batch_size = original_batch_limit
    app_state.ingest_rate_limiter = original_limiter
    app_state.interceptor.mode = original_mode


def test_root_and_health_endpoints() -> None:
    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        root_payload = root.json()
        assert root_payload["name"] == "ACI Platform"
        assert root_payload["health_url"] == "/health"

        health = client.get("/health")
        assert health.status_code == 200
        health_payload = health.json()
        assert health_payload["status"] == "healthy"

        live = client.get("/live")
        assert live.status_code == 200
        assert live.json()["status"] == "alive"

        ready = client.get("/ready")
        assert ready.status_code == 200
        ready_payload = ready.json()
        assert ready_payload["status"] == "ready"
        assert all(ready_payload["checks"].values())

        prom = client.get("/metrics/prometheus")
        assert prom.status_code == 200
        assert "text/plain" in prom.headers["content-type"]
        assert "aci_interceptor_latency_p99" in prom.text


def test_event_ingest_is_idempotent_by_source_and_key() -> None:
    event_payload = {
        "event_type": "inference.request",
        "subject_id": "req-aci-1",
        "attributes": {
            "service_name": "customer-support-bot",
            "model": "gpt-4o-mini",
            "provider": "openai",
        },
        "event_time": datetime.now(UTC).isoformat(),
        "source": "unit-test",
        "idempotency_key": "req-aci-1",
    }

    with TestClient(app) as client:
        first = client.post("/v1/events/ingest", json=event_payload)
        second = client.post("/v1/events/ingest", json=event_payload)

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["accepted"] is True
        assert second.json()["accepted"] is False


def test_event_ingest_rejects_invalid_event_payload() -> None:
    invalid_payload = {
        "event_type": "inference.request",
        "subject_id": "req-bad-1",
        "attributes": {"provider": "openai"},  # Missing required "model".
        "event_time": datetime.now(UTC).isoformat(),
        "source": "unit-test",
        "idempotency_key": "req-bad-1",
    }

    with TestClient(app) as client:
        response = client.post("/v1/events/ingest", json=invalid_payload)
        assert response.status_code == 422
        assert "Invalid payload for event_type='inference.request'" in response.json()["detail"]


def test_dashboard_overview_and_intercept_fail_open() -> None:
    with TestClient(app) as client:
        overview = client.get("/v1/dashboard/overview")
        assert overview.status_code == 200
        data = overview.json()
        assert "index_size" in data
        assert "interceptor_mode" in data

        intercept = client.post(
            "/v1/intercept",
            json={
                "request_id": "req-miss-1",
                "model": "gpt-4o",
                "provider": "openai",
                "service_name": "unknown-service",
                "api_key_id": "",
                "input_tokens": 120,
                "estimated_cost_usd": 0.012,
            },
        )
        assert intercept.status_code == 200
        assert intercept.json()["outcome"] == "fail_open"


def test_demo_bootstrap_seeds_interceptable_workloads() -> None:
    app_state = get_state()
    app_state.config.environment = "development"

    with TestClient(app) as client:
        bootstrap = client.post("/v1/demo/bootstrap")
        assert bootstrap.status_code == 200
        payload = bootstrap.json()
        assert payload["seeded_entries"] >= 3
        assert "customer-support-bot" in payload["workloads"]

        intercept = client.post(
            "/v1/intercept",
            json={
                "request_id": "req-demo-1",
                "model": "gpt-4o-mini",
                "provider": "openai",
                "service_name": "customer-support-bot",
                "input_tokens": 500,
                "max_tokens": 300,
                "estimated_cost_usd": 0.002,
                "environment": "staging",
            },
        )
        assert intercept.status_code == 200
        assert intercept.json()["outcome"] != "fail_open"


def test_demo_bootstrap_disabled_in_production() -> None:
    app_state = get_state()
    app_state.config.environment = "production"
    app_state.config.auth.enabled = False
    with TestClient(app) as client:
        response = client.post("/v1/demo/bootstrap")
        assert response.status_code == 403
        assert "disabled in production" in response.json()["detail"]


def test_intercept_endpoint_can_be_disabled_by_runtime_role_flag() -> None:
    app_state = get_state()
    app_state.accepts_interception = False
    with TestClient(app) as client:
        response = client.post(
            "/v1/intercept",
            json={
                "request_id": "req-role-off-1",
                "model": "gpt-4o",
            },
        )
        assert response.status_code == 503
        assert "interceptor disabled" in response.json()["detail"]


def test_gateway_runtime_restricts_non_intercept_v1_routes() -> None:
    app_state = get_state()
    app_state.runtime_role = "gateway"
    app_state.accepts_ingestion = False
    app_state.accepts_interception = True

    with TestClient(app) as client:
        response = client.get("/v1/dashboard/overview")
        assert response.status_code == 503
        assert "runtime role 'gateway'" in response.json()["detail"]


def test_v1_endpoints_require_auth_outside_development() -> None:
    app_state = get_state()
    app_state.config.environment = "production"
    app_state.config.auth.enabled = True
    app_state.config.auth.jwt_algorithm = "HS256"
    app_state.config.auth.jwt_hs256_secret = SecretStr("unit-test-secret-unit-test-secret-32")
    app_state.config.auth.jwt_issuer = "aci-tests"
    app_state.config.auth.jwt_audience = "aci-api"
    app_state.config.auth.required_scope = "aci.api"
    app_state.config.auth.tenant_claim = "tenant_id"

    token = encode(
        {
            "sub": "svc-gateway",
            "iss": "aci-tests",
            "aud": "aci-api",
            "tenant_id": app_state.config.tenant_id,
            "scope": "aci.api",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        key="unit-test-secret-unit-test-secret-32",
        algorithm="HS256",
    )

    with TestClient(app) as client:
        unauthenticated = client.get("/v1/dashboard/overview")
        assert unauthenticated.status_code == 401

        authenticated = client.get(
            "/v1/dashboard/overview",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert authenticated.status_code == 200


def test_v1_endpoints_allow_auth_bypass_in_demo() -> None:
    app_state = get_state()
    app_state.config.environment = "demo"
    app_state.config.auth.enabled = True
    app_state.config.auth.allow_dev_bypass = True

    with TestClient(app) as client:
        response = client.get("/v1/dashboard/overview")
        assert response.status_code == 200


def test_event_batch_ingest_rejects_oversized_batches() -> None:
    app_state = get_state()
    app_state.ingest_max_batch_size = 1

    payload = {
        "events": [
            {
                "event_type": "inference.request",
                "subject_id": "req-batch-1",
                "attributes": {"model": "gpt-4o-mini", "provider": "openai"},
                "event_time": datetime.now(UTC).isoformat(),
                "source": "unit-test",
                "idempotency_key": "req-batch-1",
            },
            {
                "event_type": "inference.request",
                "subject_id": "req-batch-2",
                "attributes": {"model": "gpt-4o-mini", "provider": "openai"},
                "event_time": datetime.now(UTC).isoformat(),
                "source": "unit-test",
                "idempotency_key": "req-batch-2",
            },
        ]
    }

    with TestClient(app) as client:
        response = client.post("/v1/events/ingest/batch", json=payload)
        assert response.status_code == 413
        assert "exceeds configured max" in response.json()["detail"]


def test_event_ingest_rate_limit_is_enforced() -> None:
    app_state = get_state()
    app_state.ingest_rate_limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)

    payload_1 = {
        "event_type": "inference.request",
        "subject_id": "req-limit-1",
        "attributes": {"model": "gpt-4o-mini", "provider": "openai"},
        "event_time": datetime.now(UTC).isoformat(),
        "source": "unit-test",
        "idempotency_key": "req-limit-1",
    }
    payload_2 = {
        "event_type": "inference.request",
        "subject_id": "req-limit-2",
        "attributes": {"model": "gpt-4o-mini", "provider": "openai"},
        "event_time": datetime.now(UTC).isoformat(),
        "source": "unit-test",
        "idempotency_key": "req-limit-2",
    }

    with TestClient(app) as client:
        first = client.post("/v1/events/ingest", json=payload_1)
        second = client.post("/v1/events/ingest", json=payload_2)

        assert first.status_code == 200
        assert second.status_code == 429
        assert "rate limit exceeded" in second.json()["detail"]


def test_index_lookup_endpoint_contract() -> None:
    workload_id = "lookup-test-svc"
    get_state().index_store.materialize(
        AttributionIndexEntry(
            workload_id=workload_id,
            team_id="team-finops",
            team_name="FinOps",
            cost_center_id="CC-9900",
            confidence=0.91,
            confidence_tier="chargeback_ready",
            method_used="R1",
            model_allowlist=["gpt-4o-mini"],
            token_budget_input=1000,
        )
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/index/lookup?key={workload_id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["key"] == workload_id
        assert payload["index_version"] >= 1
        assert payload["attribution"]["owner_id"] == "team-finops"
        assert payload["attribution"]["reason"] == "DIRECT_OWNER_MAPPING"
        assert payload["cache_ttl_ms"] == 60000
        assert len(payload["constraints"]) >= 1


def test_intercept_gate_returns_413_schema_for_input_token_limit() -> None:
    app_state = get_state()
    app_state.interceptor.mode = DeploymentMode.ACTIVE

    app_state.index_store.materialize(
        AttributionIndexEntry(
            workload_id="gate-test-svc",
            team_id="team-risk",
            team_name="Risk",
            cost_center_id="CC-2200",
            confidence=0.96,
            confidence_tier="chargeback_ready",
            method_used="R1",
            token_budget_input=100,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/intercept",
            json={
                "request_id": "req-gate-413",
                "model": "gpt-4o",
                "service_name": "gate-test-svc",
                "input_tokens": 250,
                "environment": "staging",
            },
        )
        assert response.status_code == 413
        payload = response.json()
        assert payload["error"]["type"] == "token_size_exceeded"
        assert payload["error"]["policy_id"] == "token_budget_input"
        assert payload["error"]["request_id"] == "req-gate-413"
        assert payload["error"]["retry"] is False


def test_policy_evaluate_endpoint_contract() -> None:
    policy = PolicyDefinition(
        policy_id="test-model-allowlist",
        policy_type=PolicyType.MODEL_ALLOWLIST,
        name="Test Allowlist",
        scope="global",
        enforcement=EnforcementAction.SOFT_STOP,
        parameters={"allowed_models": ["gpt-4o-mini"]},
    )
    app_state = get_state()
    app_state.policy_engine.register_policy(policy)

    with TestClient(app) as client:
        advisory = client.post(
            "/v1/policy/evaluate",
            json={
                "request_id": "req-policy-1",
                "env": "staging",
                "service_id": "svc-support",
                "model_requested": "gpt-4o",
                "attribution": {"confidence": 0.95},
                "signals": {"est_cost_usd": 3.2},
                "mode": "ADVISORY",
                "service_policy": {"active_lite_enabled": False, "allowed_actions": []},
            },
        )
        assert advisory.status_code == 200
        advisory_payload = advisory.json()
        assert advisory_payload["decision"] == "ADVISORY"
        assert advisory_payload["fail_open"] is False
        assert len(advisory_payload["advisories"]) >= 1
        assert advisory_payload["actions"] == []

        active_lite = client.post(
            "/v1/policy/evaluate",
            json={
                "request_id": "req-policy-2",
                "env": "staging",
                "service_id": "svc-support",
                "model_requested": "gpt-4o",
                "attribution": {"confidence": 0.95},
                "signals": {"est_cost_usd": 3.2},
                "mode": "ACTIVE_LITE",
                "service_policy": {
                    "active_lite_enabled": True,
                    "allowed_actions": ["MODEL_ROUTE"],
                },
            },
        )
        assert active_lite.status_code == 200
        active_payload = active_lite.json()
        assert active_payload["decision"] == "APPLY"
        assert active_payload["fail_open"] is False
        assert len(active_payload["actions"]) >= 1
