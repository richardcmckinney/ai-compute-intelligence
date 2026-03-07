# Demo Guide

This is the shortest technical path to validate platform behavior for external review.

## Objective

Demonstrate that the platform can:

1. Materialize attribution controls into low-latency serving keys.
2. Execute decision-time interception with fail-open guarantees.
3. Surface optimization and governance decisions with traceable methodology.

## Prerequisites

- Python 3.12+
- `pip`

Change into the repository root first.

If you cloned the repository:

```bash
git clone https://github.com/richardcmckinney/ai-compute-intelligence.git
cd ai-compute-intelligence
```

If you downloaded the ZIP from GitHub:

```bash
cd /path/to/where/you/unzipped/ai-compute-intelligence
```

## Run Local Demo

```bash
./scripts/run_demo.sh
```

Open:

- `http://localhost:8000/platform/`

The local launcher uses the dedicated `demo` runtime profile. It starts single-worker,
auto-seeds deterministic attribution/index state, and allows auth bypass only in `demo`.

## Recommended UI Flow

1. In the top bar, open **Execution Stream**.
2. Click **Run Full Sequence**.
3. Confirm outcomes in log stream:
- `Scenario A: Enriched`
- `Scenario B: Soft Stop`
- `Scenario C: Fail-Open`

Use **Bootstrap Demo Data** only if you want to reset the runtime back to the original
demo state during the walkthrough.

Then walk views in this order:

1. **Overview**: financial summary, spend distributions, team attribution.
2. **Attribution**: six-layer chain, request-level cost explanation, bi-directional architecture flow.
3. **Models** and **Teams**: model and org optimization surfaces.
4. **Interventions**: approve/reject/dismiss lifecycle and methodology signals.
5. **Governance**: mode controls, active policies, fail-open matrix, trust boundary notes.
6. **Forecasting**: generate live spend projection (`POST /v1/forecast/spend`).
7. **Integrations**: dispatch simulated Slack/email/webhook events.
8. **Glossary / FAQ**: diligence-ready terminology and methodology references.

## Equivalent API Walkthrough

### 1) Seed deterministic demo entries

```bash
curl -s -X POST http://localhost:8000/v1/demo/bootstrap | jq
```

The platform is already seeded on startup in demo mode; this endpoint is a reset hook.

### 2) Enriched outcome

```bash
curl -s -X POST http://localhost:8000/v1/intercept \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "demo-enriched-1",
    "model": "gpt-4o-mini",
    "provider": "openai",
    "service_name": "customer-support-bot",
    "input_tokens": 600,
    "max_tokens": 350,
    "estimated_cost_usd": 0.0032,
    "environment": "staging"
  }' | jq
```

### 3) Soft stop outcome

```bash
curl -s -X POST http://localhost:8000/v1/intercept \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "demo-soft-stop-1",
    "model": "gpt-4o",
    "provider": "openai",
    "service_name": "analytics-batch",
    "input_tokens": 5200,
    "max_tokens": 1400,
    "estimated_cost_usd": 0.014,
    "environment": "staging"
  }' | jq
```

### 4) Fail-open outcome

```bash
curl -s -X POST http://localhost:8000/v1/intercept \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "demo-fail-open-1",
    "model": "gpt-4o",
    "provider": "openai",
    "service_name": "unknown-service",
    "input_tokens": 400,
    "max_tokens": 300,
    "estimated_cost_usd": 0.004,
    "environment": "staging"
  }' | jq
```

## Operational Endpoints

- `GET /health`
- `GET /ready`
- `GET /metrics`
- `GET /metrics/prometheus`
- `GET /v1/dashboard/overview`
- `GET /v1/pricing/catalog`
- `POST /v1/pricing/estimate`
- `POST /v1/finops/synthetic`
- `POST /v1/finops/reconcile`
- `GET /v1/finops/drift`
- `POST /v1/forecast/spend`
- `GET /v1/interventions`
- `GET /v1/interventions/summary`
- `POST /v1/interventions/{intervention_id}/status`
- `POST /v1/interventions/cost-simulate`
- `POST /v1/integrations/notify`
- `GET /v1/integrations/deliveries`

## Smoke Verification

Run the end-to-end local smoke test:

```bash
./scripts/smoke_demo.sh
```
