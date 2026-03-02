# Operations and Deployment

## Environments
- `staging`: integration and pre-production checks.
- `production`: release gate for production readiness.

## Runtime Segmentation
- `gateway` role: decision-time interceptor/API; network egress limited to DNS + Redis index.
- `processor` role: async ingestion/reconciliation/materialization; egress allowed to Kafka/Neo4j/Redis.
- Circuit breaker state is shared through Redis to keep fail-open behavior consistent across pods.

## Workflows
- CI: lint, typecheck, unit, integration, glass-jaw, docker smoke.
- CodeQL: static security analysis on push/PR + weekly schedule.
- Dependency Review: PR-time dependency risk gate.
- Deployment Gate: manual workflow with environment selection and smoke tests.

## Broker and Data-Plane Reliability
- Event payloads are schema-validated before ingest.
- Durable broker mode (`KafkaEventBus`) supports DLQ routing.
- Consumer lag is surfaced as a first-class metric for backlog alerting.
- Idempotency dedup in production uses Redis TTL keys, avoiding unbounded process memory growth.

## Release Guidance
1. Merge to `main` only after CI and CodeQL pass.
2. Run Deployment Gate for `staging`.
3. Run Deployment Gate for `production`.
4. Tag release commit and publish release notes.

## Operational SLO Suggestions
- API availability: >= 99.9%
- P99 interceptor overhead: <= 15ms target
- Failed-open request ratio: continuously monitored
