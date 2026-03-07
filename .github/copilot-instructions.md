# Copilot Custom Instructions

## Project Overview

AI Compute Intelligence (ACI): an enterprise SaaS platform providing application-level cost and carbon attribution for AI inference workloads. Python 3.12 monorepo using FastAPI, Pydantic v2 (strict mode), and structlog. The codebase implements a two-tier graph architecture with a fail-open interceptor, heuristic reconciliation engine, confidence calibration, carbon calculation, and federated benchmarking protocol.

## Tech Stack

- **Runtime:** Python 3.12, FastAPI, Uvicorn
- **Validation:** Pydantic v2 with strict mode enabled across all models
- **Logging:** structlog (structured, JSON-serializable output only)
- **Linting:** Ruff (all lint and format checks must pass before merge)
- **Testing:** pytest with pytest-asyncio for async tests
- **Databases:** Neo4j (graph store, Tier 1), Redis (materialized attribution index, Tier 2)
- **Message bus:** Kafka (event bus, append-only, idempotent delivery)
- **Infrastructure:** Docker, docker-compose, Kubernetes, AWS App Runner
- **CI:** GitHub Actions

## Architecture Constraints

- The event bus is the system of record. The graph is derived state, rebuildable from the event log.
- The interceptor has a hard 20ms timeout budget for index lookups and 50ms total request budget. All decision-time operations must be O(1) with respect to attribution graph size.
- Fail-open is a safety invariant: if enrichment fails, the original request proceeds unmodified. Never introduce blocking calls or external dependencies in the interceptor hot path.
- The attribution index (Tier 2) is a precomputed materialized view served from local memory. It must never perform synchronous calls to Neo4j or any external system during request interception.

## Code Standards

- Type annotations on all function signatures, including return types. Use `X | None` union syntax, not `Optional[X]`.
- Pydantic v2 models use `model_copy()`, `model_dump()`, and `model_validate()`. Never use deprecated v1 methods (`dict()`, `copy()`, `parse_obj()`).
- All datetime values must be timezone-aware (`datetime.now(timezone.utc)`). Naive datetimes are bugs.
- Use `from __future__ import annotations` in every module for PEP 604 style annotations.
- Prefer `structlog.get_logger()` for all logging. No `print()` statements. No stdlib `logging` calls.
- Imports: standard library first, third-party second, local (`aci.*`) third. Ruff enforces this.
- No bare `except:` clauses. Catch specific exception types.
- No mutable default arguments in function signatures.
- Docstrings on all public classes and functions. Reference patent specification section numbers where applicable (e.g., "Section 3.1", "Section 4.2").
- Use `match` statements (Python 3.10+) for multi-branch type/enum dispatch where appropriate.

## Testing Standards

- All new functionality requires corresponding unit tests. Integration tests required for cross-module interactions.
- Test files mirror source layout: `src/aci/hre/combiner.py` maps to `tests/unit/test_combiner.py`.
- Use pytest fixtures from `conftest.py`. Do not instantiate shared test state inline.
- Glass Jaw tests (performance/reliability invariants) must assert: P99 latency bounds, zero-failure guarantees, and memory bounds for critical paths.
- Async tests use `@pytest.mark.asyncio` and `pytest-asyncio`.

## Package Layout

The source root is `src/aci/`. The top-level package is `aci`. There is no `src/__init__.py` (this would break the import hierarchy). All imports use `aci.*` as the base path.

## Review Priorities

When reviewing pull requests, focus on these areas in order of priority:

1. **Interceptor safety:** Any change touching `interceptor/`, the index lookup path, or decision-time routing must preserve fail-open semantics and the 20ms/50ms timeout budgets. Flag any new external calls, blocking I/O, or exception paths that could violate these invariants.
2. **Pydantic v2 compliance:** Flag any use of deprecated Pydantic v1 API methods. Ensure all models use strict mode where defined.
3. **Graph consistency:** Changes to `graph/store.py` or `hre/` must preserve time-versioned edge semantics. Closing old edges before adding new ones between the same nodes is mandatory (Section 4.2).
4. **Confidence calibration:** The calibration engine uses isotonic regression with warm-start and temporal decay. Flag any change that would silently alter calibration behavior or skip recalibration when signal distributions change.
5. **Carbon calculation:** Every carbon figure must carry an immutable calculation receipt documenting method, data sources, and uncertainty band. Flag any carbon output without a receipt.
6. **Privacy guarantees:** The federated benchmarking protocol applies differential privacy via Laplace noise. Flag any use of standard floating-point RNG for privacy-sensitive noise generation (must use secure discrete Laplace or snapping mechanism).
7. **Test coverage:** Flag any new public function or class without corresponding test coverage.

## Auto-Implementation Directive

When a review finding meets ALL of the following criteria, mark it for automatic implementation rather than leaving it as a comment requiring manual action:

- The fix is localized to a single file and does not change any public interface (function signature, class API, model schema, or endpoint contract)
- The fix does not alter control flow logic, branching conditions, or algorithmic behavior
- The fix is verifiable by existing tests passing without modification (no new tests required to validate correctness)
- The fix falls into one of these categories: missing type annotations, missing `from __future__ import annotations`, Pydantic v1-to-v2 method migration, import ordering, unused imports, bare `except` narrowing to specific exception types, `print()` replacement with structlog, naive datetime correction to timezone-aware, docstring additions, whitespace and formatting corrections, f-string conversion from `.format()` or `%` string interpolation

These are zero-risk, zero-regression changes. Apply them directly. Do not leave them as review comments for manual follow-up.

For any finding outside this list, leave a review comment with a clear explanation and a suggested code change. Do not auto-apply changes that modify behavior, alter test expectations, or touch the interceptor hot path.

## Common Patterns

```python
# Correct: timezone-aware datetime
from datetime import datetime, timezone
now = datetime.now(timezone.utc)

# Correct: Pydantic v2 model copy with updates
updated = existing_model.model_copy(update={"field": new_value})

# Correct: structured logging
import structlog
logger = structlog.get_logger()
logger.info("event.name", key="value", metric=42)

# Correct: union type syntax
def get_node(self, node_id: str) -> GraphNode | None:
    return self.nodes.get(node_id)

# Correct: fail-open pattern in interceptor
try:
    result = await asyncio.wait_for(lookup(), timeout=0.020)
except (asyncio.TimeoutError, Exception):
    return original_request  # fail-open: proceed unmodified
```

```python
# WRONG: Pydantic v1 methods (deprecated)
data = model.dict()       # use model.model_dump()
copy = model.copy()       # use model.model_copy()

# WRONG: naive datetime
now = datetime.utcnow()   # use datetime.now(timezone.utc)

# WRONG: bare except
try:
    something()
except:                    # catch specific exceptions
    pass

# WRONG: print for logging
print(f"Processing {item}")  # use logger.info("processing", item=item)
```
