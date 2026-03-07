# Operations and Deployment

## Deployment Profiles
- `demo`: deterministic single-worker profile for local product demonstrations.
- `shared-backend local`: Docker Compose profile using Kafka, Redis, and Neo4j.
- `production/staging`: role-segregated gateway and processor deployments with explicit backend selection.

## Runtime Segmentation
- `gateway` role: decision-time interceptor/API; network egress limited to DNS, Redis index, and Kafka for shadow-event publication.
- `processor` role: async ingestion/reconciliation/materialization; egress allowed to Kafka/Neo4j/Redis.
- Circuit breaker state is shared through Redis to keep fail-open behavior consistent across pods.

## Workflows
- CI: lint, typecheck, unit, integration, latency/fail-open validation, docker smoke.
- CodeQL: static security analysis on push/PR + weekly schedule.
- Dependency Review: PR-time dependency risk gate.
- Release: manual SemVer workflow for artifact assembly, tagging, and GitHub release publication.
- Cache Hygiene: scheduled workflow for GitHub Actions cache maintenance.

## Broker and Data-Plane Reliability
- Event payloads are schema-validated before ingest.
- Durable broker mode (`KafkaEventBus`) supports DLQ routing.
- Consumer lag is surfaced as a first-class metric for backlog alerting.
- Idempotency dedup in production uses Redis TTL keys, avoiding unbounded process memory growth.

## Release Guidance
1. Merge to `main` only after CI and CodeQL pass.
2. Run the `Release` workflow from the target ref.
3. Publish the immutable `v*` tag and generated release notes.
4. Promote deployment artifacts through the target environment outside the repo workflow boundary.

## Operational SLO Suggestions
- API availability: >= 99.9%
- P99 interceptor overhead: <= 15ms target
- Failed-open request ratio: continuously monitored
