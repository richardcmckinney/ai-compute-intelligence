from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from jwt import encode

from aci.api.app import SlidingWindowRateLimiter, app, state


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


def test_intercept_endpoint_can_be_disabled_by_runtime_role_flag() -> None:
    old_accepts_interception = state.accepts_interception
    state.accepts_interception = False
    try:
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
    finally:
        state.accepts_interception = old_accepts_interception


def test_v1_endpoints_require_auth_outside_development() -> None:
    original_environment = state.config.environment
    original_enabled = state.config.auth.enabled
    original_algorithm = state.config.auth.jwt_algorithm
    original_secret = state.config.auth.jwt_hs256_secret
    original_issuer = state.config.auth.jwt_issuer
    original_audience = state.config.auth.jwt_audience
    original_required_scope = state.config.auth.required_scope
    original_tenant_claim = state.config.auth.tenant_claim

    state.config.environment = "production"
    state.config.auth.enabled = True
    state.config.auth.jwt_algorithm = "HS256"
    state.config.auth.jwt_hs256_secret = "unit-test-secret-unit-test-secret-32"
    state.config.auth.jwt_issuer = "aci-tests"
    state.config.auth.jwt_audience = "aci-api"
    state.config.auth.required_scope = "aci.api"
    state.config.auth.tenant_claim = "tenant_id"

    token = encode(
        {
            "sub": "svc-gateway",
            "iss": "aci-tests",
            "aud": "aci-api",
            "tenant_id": state.config.tenant_id,
            "scope": "aci.api",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        key="unit-test-secret-unit-test-secret-32",
        algorithm="HS256",
    )

    try:
        with TestClient(app) as client:
            unauthenticated = client.get("/v1/dashboard/overview")
            assert unauthenticated.status_code == 401

            authenticated = client.get(
                "/v1/dashboard/overview",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert authenticated.status_code == 200
    finally:
        state.config.environment = original_environment
        state.config.auth.enabled = original_enabled
        state.config.auth.jwt_algorithm = original_algorithm
        state.config.auth.jwt_hs256_secret = original_secret
        state.config.auth.jwt_issuer = original_issuer
        state.config.auth.jwt_audience = original_audience
        state.config.auth.required_scope = original_required_scope
        state.config.auth.tenant_claim = original_tenant_claim


def test_event_batch_ingest_rejects_oversized_batches() -> None:
    original_batch_limit = state.ingest_max_batch_size
    state.ingest_max_batch_size = 1

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

    try:
        with TestClient(app) as client:
            response = client.post("/v1/events/ingest/batch", json=payload)
            assert response.status_code == 413
            assert "exceeds configured max" in response.json()["detail"]
    finally:
        state.ingest_max_batch_size = original_batch_limit


def test_event_ingest_rate_limit_is_enforced() -> None:
    original_limiter = state.ingest_rate_limiter
    state.ingest_rate_limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)

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

    try:
        with TestClient(app) as client:
            first = client.post("/v1/events/ingest", json=payload_1)
            second = client.post("/v1/events/ingest", json=payload_2)

            assert first.status_code == 200
            assert second.status_code == 429
            assert "rate limit exceeded" in second.json()["detail"]
    finally:
        state.ingest_rate_limiter = original_limiter
