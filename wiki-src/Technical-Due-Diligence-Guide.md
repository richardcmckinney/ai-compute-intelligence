# Technical Due Diligence Guide

This page is intended for technical reviewers evaluating architecture quality, execution rigor, and production readiness.

## Reviewer Checklist

1. Verify deterministic hot-path behavior in the interceptor.
2. Validate fail-open semantics under timeout/error scenarios.
3. Confirm strict schema validation before event-bus ingress.
4. Inspect security controls (auth, tenancy, hardening baselines).
5. Review quality gates and test coverage depth.

## What to Inspect First

- API and runtime composition: `src/aci/api/app.py`
- Interceptor safety model: `src/aci/interceptor/gateway.py`
- Event integrity and durability model: `src/aci/core/event_bus.py`
- Strict payload contracts: `src/aci/core/event_schema.py`
- Materialized index serving model: `src/aci/index/materializer.py`

## Demonstration Path

1. Launch the stack and open `/platform/`.
2. Use **Live Demo Console** to bootstrap deterministic demo data.
3. Execute the full scenario sequence and inspect outcomes:
   - Enriched request
   - Policy-driven soft stop
   - Fail-open on unknown workload
4. Cross-check with `GET /v1/dashboard/overview`, `GET /metrics`, and `GET /metrics/prometheus`.

## Strength Signals

- Clear separation between asynchronous reconciliation and decision-time interception.
- Explicitly bounded behavior around headers, token budgets, and rate limits.
- Durable-bus + deduplication semantics with DLQ and lag instrumentation.
- Strict CI profile (`ruff`, strict `mypy`, tests, static/dependency analysis).

## Residual Review Areas

- Environment-specific integrations (cloud/IdP connectors) are deployment-context dependent.
- Production secrets/KMS integration should be validated in target customer environments.
- Capacity and SLO posture should be validated with design-partner traffic envelopes.
