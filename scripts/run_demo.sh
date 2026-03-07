#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${ACI_DEMO_PORT:-8000}"

cd "${REPO_ROOT}"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements-dev.lock >/dev/null
python -m pip install --no-deps -e . >/dev/null

# Deterministic local runtime defaults for demo mode.
export ACI_ENVIRONMENT="${ACI_ENVIRONMENT:-demo}"
export ACI_RUNTIME_ROLE="${ACI_RUNTIME_ROLE:-all}"
export ACI_UVICORN_WORKERS="${ACI_UVICORN_WORKERS:-1}"
export ACI_GRAPH_BACKEND="${ACI_GRAPH_BACKEND:-memory}"
export ACI_EVENT_BUS_BACKEND="${ACI_EVENT_BUS_BACKEND:-memory}"
export ACI_INDEX_BACKEND="${ACI_INDEX_BACKEND:-memory}"
export ACI_INTERCEPTOR_CIRCUIT_STATE_BACKEND="${ACI_INTERCEPTOR_CIRCUIT_STATE_BACKEND:-local}"
export ACI_AUTH_ENABLED="${ACI_AUTH_ENABLED:-true}"
export ACI_AUTH_ALLOW_DEV_BYPASS="${ACI_AUTH_ALLOW_DEV_BYPASS:-true}"

echo "ACI demo starting on http://localhost:${PORT}/platform/"
echo "ACI runtime profile: environment=${ACI_ENVIRONMENT} workers=${ACI_UVICORN_WORKERS} graph=${ACI_GRAPH_BACKEND} event_bus=${ACI_EVENT_BUS_BACKEND} index=${ACI_INDEX_BACKEND}"

if [[ "${ACI_DEMO_RELOAD:-0}" == "1" ]]; then
  uvicorn aci.api.app:app --host 0.0.0.0 --port "${PORT}" --reload
else
  uvicorn aci.api.app:app --host 0.0.0.0 --port "${PORT}"
fi
