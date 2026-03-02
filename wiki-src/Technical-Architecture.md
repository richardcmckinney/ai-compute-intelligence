# Technical Architecture

## Runtime Components
- Event Bus: append-only, idempotency-aware ingestion fabric.
- Graph Store (Tier 1): authoritative attribution graph with time-versioned edges.
- Attribution Index (Tier 2): precomputed O(1) lookup store for interceptor path.
- Interceptor: fail-open decision-time gateway with circuit breaker.

## Data Flow
1. Ingest domain events.
2. Reconcile entities and ownership evidence.
3. Calibrate confidence.
4. Materialize precomputed index entries.
5. Intercept requests and apply advisory/active policy logic.

## Latency and Safety
- Critical path avoids graph traversal and remote I/O.
- Interceptor uses strict budgets and fail-open behavior.
- Circuit breaker prevents cascading failures.

## Recent Hardening
- Bounded LRU index store with eviction metrics.
- Graph adjacency and active-edge indexing.
- Event bus async handler support and bounded dedup memory.
- Deployment-gate workflow with environment binding.

