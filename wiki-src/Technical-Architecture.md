# Technical Architecture

## Runtime Components
- Event Bus: append-only, idempotency-aware ingestion fabric.
- Graph Store (Tier 1): authoritative attribution graph with time-versioned edges.
- Attribution Index (Tier 2): precomputed O(1) lookup store for interceptor path.
- Interceptor: fail-open decision-time gateway with circuit breaker.

## Data Flow
1. Ingest domain events.
2. Persist events into the append-only event bus.
3. Reconcile entities and ownership evidence in the HRE.
4. Write the authoritative attribution graph.
5. Materialize precomputed index entries.
6. Serve interception and reporting reads from the attribution index.

## Latency and Safety
- Critical path avoids graph traversal and remote I/O.
- Interceptor uses strict budgets and fail-open behavior.
- Circuit breaker prevents cascading failures.

## Operational Safeguards
- Strict schema validation before bus ingestion (reject malformed event payloads).
- Kafka durable bus mode with DLQ routing and consumer lag telemetry.
- Redis-backed idempotency dedup for durable at-least-once handling.
- Bounded LRU index store with eviction metrics.
- Redis durable fallback on index misses to avoid value-loss during LRU eviction.
- Redis-backed shared circuit breaker state for cross-replica consistency.
- Graph adjacency and active-edge indexing.
- HRE execution bounds (time budget, cluster caps, bounded history window, short-lived cache).
- Release workflow with immutable `v*` tag protection.
