from __future__ import annotations

from fastapi.testclient import TestClient

from aci.api.app import app, get_state
from aci.pricing.catalog import PricingUsage


def test_pricing_catalog_estimate_with_cached_tokens() -> None:
    app_state = get_state()
    estimate = app_state.pricing.estimate(
        PricingUsage(
            provider="google",
            model="gemini-2.0-flash",
            input_tokens=3000,
            output_tokens=800,
            cache_read_input_tokens=1200,
            request_count=10,
        )
    )
    assert estimate.total_cost_usd > 0
    assert estimate.cached_input_cost_usd > 0
    assert estimate.input_cost_usd > estimate.cached_input_cost_usd


def test_finops_synthetic_reconcile_and_drift_endpoints() -> None:
    app_state = get_state()
    app_state.reconciliation.clear()
    with TestClient(app) as client:
        synthetic = client.post(
            "/v1/finops/synthetic",
            json={
                "request_id": "finops-req-1",
                "service_name": "analytics-batch",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "input_tokens": 2400,
                "output_tokens": 900,
            },
        )
        assert synthetic.status_code == 200
        synthetic_payload = synthetic.json()
        assert synthetic_payload["request_id"] == "finops-req-1"
        assert synthetic_payload["synthetic_cost_usd"] > 0
        assert synthetic_payload["reconciled"] is False

        reconciled = client.post(
            "/v1/finops/reconcile",
            json={
                "request_id": "finops-req-1",
                "reconciled_cost_usd": synthetic_payload["synthetic_cost_usd"] * 0.92,
                "source": "aws_cur",
            },
        )
        assert reconciled.status_code == 200
        reconciled_payload = reconciled.json()
        assert reconciled_payload["drift_usd"] != 0
        assert reconciled_payload["source"] == "aws_cur"

        drift = client.get("/v1/finops/drift?group_by=provider")
        assert drift.status_code == 200
        rows = drift.json()
        assert rows
        assert rows[0]["group"] == "openai"
        assert rows[0]["reconciled_records"] >= 1
        assert rows[0]["window"] == "daily"
        assert rows[0]["threshold_pct"] == 3.0
        assert rows[0]["dashboard_annotation"] in {"reconciled", "unreconciled"}


def test_spend_forecast_endpoint() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/forecast/spend",
            json={
                "monthly_spend_usd": [412000, 489000, 567000, 634000, 718000, 791000, 847000],
                "horizon_months": 3,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["trend_pct"] > 0
        assert len(payload["points"]) == 3
        assert payload["points"][0]["predicted_spend_usd"] > 0


def test_intervention_cost_simulation_endpoint() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/interventions/cost-simulate",
            json={
                "service_id": "svc-copilot",
                "provider": "openai",
                "current_model": "gpt-4o",
                "avg_input_tokens": 1200,
                "avg_output_tokens": 400,
                "requests_per_day": 50000,
                "candidate_models": ["gpt-4o-mini"],
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["projected_monthly_cost_usd"] > 0
        assert payload["candidates"]
        assert payload["candidates"][0]["model"] == "gpt-4o-mini"
        assert payload["candidates"][0]["monthly_savings_usd"] > 0


def test_notifications_endpoints_simulated_delivery() -> None:
    app_state = get_state()
    app_state.notification_hub.clear()
    with TestClient(app) as client:
        send = client.post(
            "/v1/integrations/notify",
            json={
                "event_type": "policy_violation",
                "title": "Budget ceiling exceeded",
                "detail": "Team Product exceeded staging threshold",
                "severity": "warning",
                "channels": ["slack", "email", "webhook"],
                "email_to": ["finops@example.com"],
            },
        )
        assert send.status_code == 200
        deliveries = send.json()
        assert len(deliveries) == 3
        assert all(item["status"] in {"simulated", "failed"} for item in deliveries)

        listed = client.get("/v1/integrations/deliveries?limit=10")
        assert listed.status_code == 200
        listed_payload = listed.json()
        assert len(listed_payload) >= 3
