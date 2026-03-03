from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from aci.api.app import app, state


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
        "attributes": {"service_name": "customer-support-bot", "model": "gpt-4o-mini", "provider": "openai"},
        "event_time": datetime.now(timezone.utc).isoformat(),
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
        "event_time": datetime.now(timezone.utc).isoformat(),
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
