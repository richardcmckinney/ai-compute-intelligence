from __future__ import annotations

from fastapi.testclient import TestClient

from aci.api.app import app, get_state
from aci.interventions.registry import InterventionRegistry


def test_registry_transitions_and_summary_metrics() -> None:
    registry = InterventionRegistry.with_seed_data()

    baseline = registry.summary()
    assert baseline.total_count >= 4
    assert baseline.recommended_count >= 1

    updated = registry.transition(
        intervention_id="INT-401",
        next_status="approved",
        actor="unit-test",
        note="approved in test",
    )
    assert updated.status == "approved"
    assert updated.updated_by == "unit-test"

    summary = registry.summary()
    assert summary.approved_count >= 1
    assert summary.captured_savings_usd > 0


def test_intervention_lifecycle_api_endpoints() -> None:
    app_state = get_state()
    original_registry = app_state.intervention_registry
    app_state.intervention_registry = InterventionRegistry.with_seed_data()

    try:
        with TestClient(app) as client:
            listing = client.get("/v1/interventions")
            assert listing.status_code == 200
            payload = listing.json()
            assert payload["summary"]["total_count"] >= 4
            assert len(payload["interventions"]) >= 4

            filtered = client.get("/v1/interventions", params={"status": "recommended"})
            assert filtered.status_code == 200
            assert all(
                row["status"] == "recommended"
                for row in filtered.json()["interventions"]
            )

            invalid_filter = client.get("/v1/interventions", params={"status": "invalid"})
            assert invalid_filter.status_code == 422

            transition = client.post(
                "/v1/interventions/INT-401/status",
                json={
                    "status": "implemented",
                    "actor": "unit-test",
                    "note": "rolled out",
                },
            )
            assert transition.status_code == 200
            transition_payload = transition.json()
            assert transition_payload["status"] == "implemented"
            assert transition_payload["updated_by"] == "unit-test"

            summary = client.get("/v1/interventions/summary")
            assert summary.status_code == 200
            assert summary.json()["implemented_count"] >= 1

            missing = client.post(
                "/v1/interventions/INT-999/status",
                json={"status": "approved"},
            )
            assert missing.status_code == 404
    finally:
        app_state.intervention_registry = original_registry
