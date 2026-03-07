# Frontend Demo

Canonical demo UI:

- `frontend/index.html`
- `frontend/data/demo_dataset.json`

Served by FastAPI at:

- `http://localhost:8000/platform/`

## Quick Start

From repo root:

```bash
./scripts/run_demo.sh
```

Custom port:

```bash
ACI_DEMO_PORT=8010 ./scripts/run_demo.sh
```

The launcher uses the dedicated `demo` runtime profile and auto-seeds the backend state.

## Primary Walkthrough

1. Open `http://localhost:8000/platform/`
2. Use **Execution Stream** (top bar, right side)
3. Click **Run Full Sequence**
4. Review:
- Overview (financial summary and organizational spend)
- Attribution (six-layer chain + request cost explanation)
- Interventions (stateful lifecycle + methodology)
- Governance (modes, policies, fail-open matrix)

Use **Bootstrap Demo Data** only when you want to reset the runtime during a session.

## Demo Guarantees

- Persistent `SIMULATED DATA` watermark
- Session-persistent intervention status transitions
- No carbon or benchmarking content in the presentation layer
- Request-level technical detail isolated to Attribution + Execution Stream drawer
