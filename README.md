# AI Compute Intelligence Platform

Application-level cost and carbon attribution for enterprise AI workloads.

## Problem

Enterprises spend tens to hundreds of thousands monthly on AI services but cannot attribute costs below the cloud billing account level. Cloud providers report what was spent; they do not report *who* spent it, *why*, or whether it was necessary. Existing FinOps tools operate on billing data alone, without the identity resolution, confidence governance, or decision-time intervention required for workload-level cost control.

## Architecture

The platform deploys as containerized microservices within the customer's VPC. Raw telemetry never leaves the customer's trust boundary. The system is organized around five subsystems connected by an append-only event bus.

### Data Flow

```
Phase 1: Ingestion     OTel collectors + cloud billing APIs -> event bus
Phase 2: Reconciliation HRE processes events, resolves entities across systems
Phase 3: Attribution    Graph traversal, fractional attribution, confidence scores
Phase 4: Materialization Index builder emits precomputed entries to serving cache
Phase 5: Interception   Fail-open interceptor reads index via O(1) lookups
Phase 6: Reporting      Dashboards, compliance exports, federated benchmarks
```

### Runtime Upgrades (v0.2)

- `InMemoryEventBus` now supports mixed sync/async handlers, bounded idempotency-key memory, and batch publish accounting.
- `GraphStore` now maintains adjacency indexes and active-edge indexes to avoid full edge-list scans as graph size grows.
- `AttributionIndexStore` now uses bounded LRU retention with eviction metrics to prevent unbounded memory growth.
- `CircuitBreaker` half-open behavior now enforces explicit probe limits before reopening/closing.
- `FailOpenInterceptor` now resolves workload IDs more robustly and hardens shadow-event emission behavior.

### Core Innovations

**Heuristic Reconciliation Engine (HRE):** Derives entity linkages from dirty, disconnected enterprise systems using six reconciliation methods (R1-R6) with calibrated confidence scores. Probabilistic reconciliation is the primary attribution method; deterministic paths are a special case.

**Confidence Governance:** Empirical calibration via isotonic regression. A confidence score of 0.80 means that among all attributions historically assigned 0.80, approximately 80% were correct. Three operational tiers: chargeback-ready (>= 0.80), provisional (0.50-0.79), estimated (< 0.50).

**Fail-Open Decision-Time Interceptor:** Intercepts AI inference requests with a hard 20ms timeout budget. Reads only from a precomputed attribution index (O(1) lookups). If enrichment cannot complete in time, the request proceeds unmodified. The system never degrades customer application performance.

**TRAC (Total Risk-Adjusted Cost):** Unified metric expressing cost, carbon liability, and attribution uncertainty in financial terms. TRAC = Billed Cost + Carbon Liability + Confidence Risk Premium.

**Federated Benchmarking Protocol:** Cross-organizational performance comparison with formal differential privacy guarantees (Laplace mechanism, privacy budget accounting, anti-differencing rules).

## Project Structure

```
src/aci/
  config.py              Platform configuration with documented defaults
  models/
    events.py            Domain events for append-only event bus
    graph.py             Graph node/edge types with time-versioned edges
    attribution.py       Attribution index entries and explanation artifacts
    confidence.py        Calibration curves, ground truth labels
    carbon.py            Carbon ledger, TRAC, policy, equivalence models
  hre/
    methods.py           R1-R6 reconciliation methods
    combination.py       Noisy-OR evidence combination with dependency penalties
    engine.py            HRE orchestrator
  confidence/
    calibration.py       Isotonic regression calibration with warm-start
  graph/
    store.py             Graph store abstraction (Neo4j in production)
  index/
    materializer.py      Attribution index materialization and O(1) serving
  interceptor/
    gateway.py           Fail-open interceptor with circuit breaker
    circuit_breaker.py   Circuit breaker state machine
  equivalence/
    verifier.py          Capability equivalence (policy/empirical/judge modes)
  trac/
    calculator.py        TRAC computation with temporal decay
  carbon/
    calculator.py        Three-layer carbon methodology with receipts
  policy/
    engine.py            Governance policy evaluation
  benchmark/
    federated.py         FBP with differential privacy
  ingestion/
    connectors/
      aws.py             AWS CUR, CloudTrail, Bedrock connectors
  core/
    event_bus.py         Append-only event bus abstraction
  api/
    app.py               FastAPI application and routes

tests/
  unit/
    test_hre.py                           HRE methods R1-R6 + combination + orchestrator
    test_confidence_trac_interceptor.py   Calibration, TRAC, interceptor
    test_equivalence_carbon_fbp.py        Equivalence, carbon, federated benchmarking
  glass_jaw/
    test_phase0_criteria.py               Phase 0 pass/fail validation
    locustfile.py                         72-hour sustained load test

k8s/
  base/deployment.yaml   Kubernetes deployment with security controls
```

## Phase 0: Glass Jaw Validation

Before any graph construction or dashboard development, the interceptor hypothesis must be validated against quantitative criteria:

| Criterion | Target | Rationale |
|-----------|--------|-----------|
| P99 latency overhead | <= 15ms | Customer API latency budget |
| Request failures | Zero over 72h | Production reliability |
| Prompt classification | >= 80% accuracy | Routing viability |
| Memory footprint | <= 256MB | Container resource limits |
| Fail-open chaos | Zero client errors | Safety guarantee |

**If any criterion fails:** redesign, pivot to dashboard-only, or narrow scope.

Run Phase 0 tests:

```bash
# Unit-scale validation (seconds).
pytest tests/glass_jaw/ -v -m glass_jaw

# Sustained load validation (72 hours).
locust -f tests/glass_jaw/locustfile.py --host=http://localhost:8000 \
    --users=500 --spawn-rate=50 --run-time=72h --headless
```

## Development

```bash
# Install with dev dependencies.
pip install -e ".[dev]"

# Run all tests.
pytest tests/ -v

# Lint and type check.
ruff check src/ tests/
mypy src/aci/ --ignore-missing-imports

# Run the API server.
uvicorn aci.api.app:app --reload --port 8000
```

## Frontend Mockup

The repository now includes two platform mockups:

- `frontend/platform-mockup-v3.html`: original source artifact you provided.
- `frontend/index.html`: improved v4 mockup with stronger information hierarchy, mobile responsiveness, and live fetch to `/v1/dashboard/overview`.

When running the API, the mockup is served at:

```bash
http://localhost:8000/platform/
```

## Key Design Decisions

**Infrastructure, not tooling.** The platform is designed to become invisible, ubiquitous, and assumed within the enterprise AI stack, not to remain a visible, optional tool that is periodically evaluated.

**Optimizer, not cost cop.** The system helps teams make better decisions about AI spend. It does not unilaterally block or restrict. Hard stops require explicit opt-in. The default posture is always soft stop (alert + log, request proceeds).

**Confidence as a first-class citizen.** Every attribution carries a calibrated confidence score. The system never presents a probabilistic guess as a deterministic fact. Uncertainty is visible, quantified, and operationally bounded.

**Fail-open by design.** The platform adds zero risk to customer applications. If any component fails, requests proceed unmodified. The interceptor is engineered to degrade gracefully under every failure mode.

## Technology Stack

- **Language:** Python 3.12+
- **API:** FastAPI + Uvicorn
- **Graph Store:** Neo4j (production), in-memory (development)
- **Event Bus:** Apache Kafka (production), in-memory (development)
- **Index Cache:** Redis with local process cache
- **ML:** scikit-learn (isotonic regression), NumPy, SciPy
- **Observability:** OpenTelemetry, Prometheus metrics
- **Infrastructure:** Kubernetes (EKS/AKS/GKE), deployed in customer VPC
